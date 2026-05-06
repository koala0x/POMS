from __future__ import annotations

"""
原始帖子表（twitter_posts / binance_square_posts）仓储基类。

现实情况：
- 两张原始表是“已有表”，字段名可能和需求文档略有差异
- 某些环境可能没有 posted_at / author / created_at 等字段

本基类会在首次访问时探测表结构并做字段映射：
- 必须存在 id（作为主键/稳定排序）
- content/is_summarized 尽量匹配，否则使用默认字段名（若不存在则会在 SQL 执行时报错）
- created_at 不存在时按 id 排序，保证批处理可重复/稳定

同时为防止 SQL 注入，表名/列名仅允许安全标识符模式，并通过 psycopg2.sql.Identifier 拼接。
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

    这是“弱兼容”策略：尽量适配不同数据源/历史版本的字段命名。
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
        # table_name 来自代码写死的常量（twitter_posts / binance_square_posts）。
        # 仍然做一次白名单正则校验，防止后续维护中误用字符串拼接导致风险。
        self._table_name = table_name
        self._loaded = False
        self._id_col: str = "id"
        self._content_col: str = "content"
        self._author_col: str | None = "author"
        self._posted_at_col: str | None = "posted_at"
        self._created_at_col: str | None = "created_at"
        self._is_summarized_col: str = "is_summarized"

    def _ensure_loaded(self, conn: psycopg2.extensions.connection) -> None:
        # 首次调用时探测表结构并确定列映射；后续直接复用映射结果。
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

        if "id" not in cols:
            # 没有 id 就无法保证“稳定取最早 N 条”，也无法做 ANY(ids) 批量标记。
            raise RuntimeError(f"{self._table_name} 缺少 id 字段")

        # 下面是“尽量匹配”的候选列集合。匹配不到就使用默认值：
        # - content/is_summarized 仍可能不存在：此时 SQL 会报错，日志可用于定位差异
        self._id_col = "id"
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
            self._table_name,
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
            t=sql.Identifier(self._table_name),
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
            t=sql.Identifier(self._table_name),
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
            t=sql.Identifier(self._table_name),
            id_col=sql.Identifier(self._id_col),
            flag=sql.Identifier(self._is_summarized_col),
        )
        with conn.cursor() as cur:
            cur.execute(q, (list(ids),))
            return int(cur.rowcount)
