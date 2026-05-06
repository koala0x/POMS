from __future__ import annotations

"""
summary_level1 表的仓储。

提供三类操作:
- insert:写入一条一次摘要,返回新生成的 id
- fetch_unsummarized_for_period:按 [period_start, period_end) 时间窗拉取
  指定 source 下还未做二次摘要的记录
- mark_summarized_l2:把指定 id 标记为已被二次摘要消费(幂等)
"""

from datetime import datetime
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from db.models import SummaryLevel1


class Level1Repo:
    def insert(
        self,
        session: Session,
        source: str,
        summary: str,
        raw_ids: Sequence[int],
        raw_count: int,
        created_at: datetime,
    ) -> int:
        """
        插入一条一次摘要,返回新 id。

        - flush() 触发 INSERT 并回填自增 id,但不 commit
        - 由上层(Service)在事务边界统一 commit
        """
        obj = SummaryLevel1(
            source=source,
            summary=summary,
            raw_ids=list(raw_ids),
            raw_count=int(raw_count),
            created_at=created_at,
        )
        session.add(obj)
        session.flush()
        return int(obj.id)

    def fetch_unsummarized_for_period(
        self,
        session: Session,
        source: str,
        period_start: datetime,
        period_end: datetime,
    ) -> list[SummaryLevel1]:
        """
        拉取过去一段时间内未做二次摘要的 level1。

        时间窗口为半开区间 [period_start, period_end),保证整点切分时不会重复计入。
        """
        stmt = (
            select(SummaryLevel1)
            .where(
                SummaryLevel1.source == source,
                SummaryLevel1.is_summarized_l2.is_(False),
                SummaryLevel1.created_at >= period_start,
                SummaryLevel1.created_at < period_end,
            )
            .order_by(SummaryLevel1.created_at.asc())
        )
        return list(session.scalars(stmt).all())

    def mark_summarized_l2(
        self, session: Session, ids: Sequence[int]
    ) -> int:
        """幂等标记为已二次摘要,返回实际被翻为 TRUE 的行数。"""
        if not ids:
            return 0
        stmt = (
            update(SummaryLevel1)
            .where(
                SummaryLevel1.id.in_(list(ids)),
                SummaryLevel1.is_summarized_l2.is_(False),
            )
            .values(is_summarized_l2=True)
        )
        result = session.execute(stmt)
        return int(result.rowcount or 0)
