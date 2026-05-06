from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Sequence

import psycopg2


@dataclass(frozen=True)
class RawPost:
    id: int
    content: str
    author: str | None
    posted_at: datetime | None
    created_at: datetime


class TwitterRepo:
    def count_unsummarized(self, conn: psycopg2.extensions.connection) -> int:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(1) FROM twitter_posts WHERE is_summarized = FALSE;")
            (cnt,) = cur.fetchone()
            return int(cnt)

    def fetch_oldest_unsummarized(
        self, conn: psycopg2.extensions.connection, limit: int
    ) -> List[RawPost]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, author, posted_at, created_at
                FROM twitter_posts
                WHERE is_summarized = FALSE
                ORDER BY created_at ASC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [RawPost(*row) for row in rows]

    def mark_summarized(
        self, conn: psycopg2.extensions.connection, ids: Sequence[int]
    ) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE twitter_posts
                SET is_summarized = TRUE
                WHERE id = ANY(%s) AND is_summarized = FALSE;
                """,
                (list(ids),),
            )
            return int(cur.rowcount)
