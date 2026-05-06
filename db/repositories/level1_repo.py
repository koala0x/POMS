from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Sequence

import psycopg2


@dataclass(frozen=True)
class Level1Summary:
    id: int
    source: str
    summary: str
    raw_ids: list[int]
    raw_count: int
    created_at: datetime
    is_summarized_l2: bool


class Level1Repo:
    def insert(
        self,
        conn: psycopg2.extensions.connection,
        source: str,
        summary: str,
        raw_ids: Sequence[int],
        raw_count: int,
        created_at: datetime,
    ) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO summary_level1 (source, summary, raw_ids, raw_count, created_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (source, summary, list(raw_ids), int(raw_count), created_at),
            )
            (new_id,) = cur.fetchone()
            return int(new_id)

    def fetch_unsummarized_for_period(
        self,
        conn: psycopg2.extensions.connection,
        source: str,
        period_start: datetime,
        period_end: datetime,
    ) -> List[Level1Summary]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source, summary, raw_ids, raw_count, created_at, is_summarized_l2
                FROM summary_level1
                WHERE source = %s
                  AND is_summarized_l2 = FALSE
                  AND created_at >= %s
                  AND created_at < %s
                ORDER BY created_at ASC;
                """,
                (source, period_start, period_end),
            )
            rows = cur.fetchall()
            return [Level1Summary(*row) for row in rows]

    def mark_summarized_l2(
        self, conn: psycopg2.extensions.connection, ids: Sequence[int]
    ) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE summary_level1
                SET is_summarized_l2 = TRUE
                WHERE id = ANY(%s) AND is_summarized_l2 = FALSE;
                """,
                (list(ids),),
            )
            return int(cur.rowcount)
