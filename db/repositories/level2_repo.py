from __future__ import annotations

"""
summary_level2 表的仓储。

只暴露 insert:每个整点会写入一条二次摘要,记录覆盖的时间窗与一次摘要 id 列表。
"""

from datetime import datetime
from typing import Sequence

from sqlalchemy.orm import Session

from db.models import SummaryLevel2


class Level2Repo:
    def insert(
        self,
        session: Session,
        source: str,
        summary: str,
        level1_ids: Sequence[int],
        level1_count: int,
        period_start: datetime,
        period_end: datetime,
        created_at: datetime,
    ) -> int:
        """插入一条二次摘要,返回新 id。"""
        obj = SummaryLevel2(
            source=source,
            summary=summary,
            level1_ids=list(level1_ids),
            level1_count=int(level1_count),
            period_start=period_start,
            period_end=period_end,
            created_at=created_at,
        )
        session.add(obj)
        session.flush()
        return int(obj.id)
