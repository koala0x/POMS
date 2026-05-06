from __future__ import annotations

from datetime import datetime
from typing import Sequence

import psycopg2


class Level2Repo:
    def insert(
        self,
        conn: psycopg2.extensions.connection,
        source: str,
        summary: str,
        level1_ids: Sequence[int],
        level1_count: int,
        period_start: datetime,
        period_end: datetime,
        created_at: datetime,
    ) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO summary_level2
                    (source, summary, level1_ids, level1_count, period_start, period_end, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (
                    source,
                    summary,
                    list(level1_ids),
                    int(level1_count),
                    period_start,
                    period_end,
                    created_at,
                ),
            )
            (new_id,) = cur.fetchone()
            return int(new_id)
