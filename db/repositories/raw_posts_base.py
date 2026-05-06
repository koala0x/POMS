from __future__ import annotations

"""
原始帖子表（twitter_posts / binance_square_posts）仓储基类。

要解决的问题：
- 原始表可能在不同 schema（不一定在当前 search_path）
- 字段命名可能不一致，甚至缺少 author/posted_at/created_at
- 仍要保证批处理“稳定可重复”（排序可复现、更新幂等）

做法：
- 首次访问时从 information_schema 探测表结构并缓存字段映射
- 表不在 search_path 时尝试定位 schema（public 优先；多 schema 则报错提示）
- created_at 缺失时退化为按主键列排序

安全性：
- 表名/列名只允许安全标识符，并通过 psycopg2.sql.Identifier 拼接，避免 SQL 注入
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Sequence

import psycopg2
from loguru import logger
from psycopg2 import sql


@dataclass(frozen=True)
class RawPost:
    """
    业务层需要的“原始帖子最小字段集合”。

    注意：
    - posted_at / author / created_at 可能不存在，因此允许为 None
    - content 需要尽量映射到正确列，否则 prompt 质量会受影响
    """

    id: int
    content: str
    author: str | None
    posted_at: datetime | None
    created_at: datetime | None


_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _pick_first(existing: set[str], candidates: Sequence[str]) -> str | None:
    """
    从 candidates 中按顺序选出第一个存在于 existing 的列名。
    """
    for c in candidates:
        if c in existing:
            return c
    return None


class RawPostsRepoBase:
    """
    原始表仓储基类：负责 count / fetch / mark 三类操作。

    字段映射会缓存到实例中（_loaded 标记），避免每次都访问 information_schema。
    """

    def __init__(self, table_name: str) -> None:
        # table_name 来自代码常量（twitter_posts / binance_square_posts），仍做一次校验。
        self._table_name = table_name
        self._schema: str | None = None
        self._loaded = False
        self._id_col: str = "id"
        self._content_col: str = "content"
        self._author_col: str | None = "author"
        self._posted_at_col: str | None = "posted_at"
        self._created_at_col: str | None = "created_at"
        self._is_summarized_col: str = "is_summarized"

    def _table_identifier(self) -> sql.Composed:
        # 已定位 schema 时使用 schema.table，避免依赖 search_path。
        if self._schema:
            return sql.Identifier(self._schema, self._table_name)
        return sql.Identifier(self._table_name)

    def _ensure_loaded(self, conn: psycopg2.extensions.connection) -> None:
        # 首次调用时探测表结构并确定列映射；后续直接复用结果。
        if self._loaded:
            return

        if not _SAFE_IDENT_RE.match(self._table_name):
            raise ValueError(f"非法表名：{self._table_name}")

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = %s
                  AND table_schema = ANY (current_schemas(true));
                """,
                (self._table_name,),
            )
            cols = {row[0] for row in cur.fetchall()}

            if not cols:
                cur.execute(
                    """
                    SELECT table_schema
                    FROM information_schema.tables
                    WHERE table_name = %s
                      AND table_type = 'BASE TABLE';
                    """,
                    (self._table_name,),
                )
                schemas = [row[0] for row in cur.fetchall()]
                if not schemas:
                    raise RuntimeError(f"{self._table_name} 表不存在（或当前连接的数据库里没有该表）")

                if "public" in schemas:
                    self._schema = "public"
                elif len(schemas) == 1:
                    self._schema = schemas[0]
                else:
                    raise RuntimeError(
                        f"{self._table_name} 表存在于多个 schema：{', '.join(sorted(schemas))}，"
                        f"请调整 search_path 或将表移动/重命名以保证唯一"
                    )

                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = %s
                      AND table_schema = %s;
                    """,
                    (self._table_name, self._schema),
                )
                cols = {row[0] for row in cur.fetchall()}

        id_col = _pick_first(
            cols,
            [
                "id",
                "post_id",
                "tweet_id",
                "status_id",
                "message_id",
                "pk_id",
                "pk",
            ],
        )
        if id_col is None:
            existing = ", ".join(sorted(cols)[:30])
            raise RuntimeError(f"{self._table_name} 未找到主键字段（候选 id/post_id/...），现有列：{existing}")

        # 下面是“尽量匹配”的候选列集合。匹配不到就使用默认值：
        # - content/is_summarized 仍可能不存在：此时 SQL 会报错，日志可用于定位差异
        self._id_col = id_col
        self._content_col = (
            _pick_first(cols, ["content", "text", "body", "message", "post", "tweet"])
            or "content"
        )
        self._author_col = _pick_first(cols, ["author", "username", "user", "screen_name"])
        self._posted_at_col = _pick_first(cols, ["posted_at", "posted_time", "post_time", "published_at"])
        self._created_at_col = _pick_first(cols, ["created_at", "created_time", "inserted_at", "inserted_time"])
        self._is_summarized_col = (
            _pick_first(
                cols,
                ["is_summarized", "summarized", "is_summary", "is_summarised", "summary_done"],
            )
            or "is_summarized"
        )

        # 再次对最终映射结果做安全校验，保证只会拼接安全标识符。
        for ident in [
            self._id_col,
            self._content_col,
            self._is_summarized_col,
            self._author_col,
            self._posted_at_col,
            self._created_at_col,
        ]:
            if ident is None:
                continue
            if not _SAFE_IDENT_RE.match(ident):
                raise ValueError(f"非法字段名：{ident}")

        # 打印一次映射结果，便于上线后快速确认“实际字段”与“代码预期”是否一致。
        logger.info(
            "[{}] 字段映射：id={} content={} author={} posted_at={} created_at={} flag={}",
            f"{self._schema + '.' if self._schema else ''}{self._table_name}",
            self._id_col,
            self._content_col,
            self._author_col,
            self._posted_at_col,
            self._created_at_col,
            self._is_summarized_col,
        )
        self._loaded = True

    def count_unsummarized(self, conn: psycopg2.extensions.connection) -> int:
        # 统计未处理条数，用于决定是否触发一次摘要（>= batch_size）。
        self._ensure_loaded(conn)
        q = sql.SQL("SELECT COUNT(1) FROM {t} WHERE {flag} = FALSE;").format(
            t=self._table_identifier(),
            flag=sql.Identifier(self._is_summarized_col),
        )
        with conn.cursor() as cur:
            cur.execute(q)
            (cnt,) = cur.fetchone()
            return int(cnt)

    def fetch_oldest_unsummarized(
        self, conn: psycopg2.extensions.connection, limit: int
    ) -> List[RawPost]:
        # 拉取最早的 limit 条未摘要数据（用于拼 prompt）。
        self._ensure_loaded(conn)

        # 对于可能不存在的列（author/posted_at/created_at），用 NULL 占位，
        # 保证结果列数固定，业务层逻辑无需为“缺列”写分支。
        author_expr = (
            sql.Identifier(self._author_col) if self._author_col is not None else sql.SQL("NULL")
        )
        posted_at_expr = (
            sql.Identifier(self._posted_at_col)
            if self._posted_at_col is not None
            else sql.SQL("NULL")
        )
        created_at_expr = (
            sql.Identifier(self._created_at_col)
            if self._created_at_col is not None
            else sql.SQL("NULL")
        )
        # 排序优先 created_at（更符合“按入库时间最早”），缺失时退化为 id。
        order_by_expr = (
            sql.Identifier(self._created_at_col)
            if self._created_at_col is not None
            else sql.Identifier(self._id_col)
        )

        q = sql.SQL(
            """
            SELECT {id_col}, {content_col}, {author_col}, {posted_at_col}, {created_at_col}
            FROM {t}
            WHERE {flag} = FALSE
            ORDER BY {order_by} ASC
            LIMIT %s;
            """
        ).format(
            id_col=sql.Identifier(self._id_col),
            content_col=sql.Identifier(self._content_col),
            author_col=author_expr,
            posted_at_col=posted_at_expr,
            created_at_col=created_at_expr,
            t=self._table_identifier(),
            flag=sql.Identifier(self._is_summarized_col),
            order_by=order_by_expr,
        )

        with conn.cursor() as cur:
            cur.execute(q, (limit,))
            rows = cur.fetchall()
            return [RawPost(*row) for row in rows]

    def mark_summarized(
        self, conn: psycopg2.extensions.connection, ids: Sequence[int]
    ) -> int:
        # 幂等标记：只把当前仍为 FALSE 的记录置 TRUE，避免重复更新影响 rowcount 判断。
        self._ensure_loaded(conn)
        q = sql.SQL(
            """
            UPDATE {t}
            SET {flag} = TRUE
            WHERE {id_col} = ANY(%s) AND {flag} = FALSE;
            """
        ).format(
            t=self._table_identifier(),
            id_col=sql.Identifier(self._id_col),
            flag=sql.Identifier(self._is_summarized_col),
        )
        with conn.cursor() as cur:
            cur.execute(q, (list(ids),))
            return int(cur.rowcount)
