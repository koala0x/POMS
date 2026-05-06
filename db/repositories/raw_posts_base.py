from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Sequence

import psycopg2
from loguru import logger
from psycopg2 import sql


@dataclass(frozen=True)
class RawPost:
    id: int
    content: str
    author: str | None
    posted_at: datetime | None
    created_at: datetime | None


_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _pick_first(existing: set[str], candidates: Sequence[str]) -> str | None:
    for c in candidates:
        if c in existing:
            return c
    return None


class RawPostsRepoBase:
    def __init__(self, table_name: str) -> None:
        self._table_name = table_name
        self._loaded = False
        self._id_col: str = "id"
        self._content_col: str = "content"
        self._author_col: str | None = "author"
        self._posted_at_col: str | None = "posted_at"
        self._created_at_col: str | None = "created_at"
        self._is_summarized_col: str = "is_summarized"

    def _ensure_loaded(self, conn: psycopg2.extensions.connection) -> None:
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
            raise RuntimeError(f"{self._table_name} 缺少 id 字段")

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
        self._ensure_loaded(conn)

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
