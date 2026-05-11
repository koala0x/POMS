from __future__ import annotations

"""
`entity_mentions` 表的仓储。

主要使用方：
- EntityExtractor：写入（ON CONFLICT DO NOTHING）
- HotnessService：查询（count_since / count_for_entity / count_sources_for_entity）
- SlidingCounter：启动回填时流式读（stream_mentions_since）
"""

from datetime import datetime
from typing import Iterator

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.models import EntityMention


class EntityMentionsRepo:
    """`entity_mentions` CRUD。"""

    # -------------------------------------------------------------------
    # 写入
    # -------------------------------------------------------------------

    def bulk_upsert(
        self,
        session: Session,
        rows: list[dict],
    ) -> int:
        """
        批量写入实体提及记录。

        `(msg_id, entity)` 冲突时 DO NOTHING（Req 4.8 幂等）。
        `rows` 中每个 dict 必须包含：
          - msg_id / entity / entity_type / raw_source / ts / confidence
        可选：engagement / author_weight / is_kol_mention

        返回 SQLAlchemy 报告的 rowcount；由于是 ON CONFLICT DO NOTHING，
        rowcount 可能低于 len(rows)，调用方一般不依赖这个返回值。
        """
        if not rows:
            return 0

        stmt = pg_insert(EntityMention).values(rows).on_conflict_do_nothing(
            constraint="uq_entity_mentions_msg_entity"
        )
        result = session.execute(stmt)
        return int(result.rowcount or 0)

    # -------------------------------------------------------------------
    # 聚合查询（供 HotnessService 用）
    # -------------------------------------------------------------------

    def count_since(self, session: Session, since: datetime) -> int:
        """
        返回 ts >= since 的总记录数。

        用于 HotnessService 的基线充足性检查（Req 7.7）：
        最近 7 天记录 < 100 → 降级跳过本轮。
        """
        stmt = (
            select(func.count())
            .select_from(EntityMention)
            .where(EntityMention.ts >= since)
        )
        return int(session.scalar(stmt) or 0)

    def count_for_entity(
        self,
        session: Session,
        entity: str,
        *,
        start: datetime,
        end: datetime,
    ) -> int:
        """
        返回某实体在 [start, end) 区间内的提及次数。

        用于基线/短窗计数。end 是开区间（遵循 Python 习惯），避免整点双算。
        """
        stmt = (
            select(func.count())
            .select_from(EntityMention)
            .where(
                EntityMention.entity == entity,
                EntityMention.ts >= start,
                EntityMention.ts < end,
            )
        )
        return int(session.scalar(stmt) or 0)

    def count_sources_for_entity(
        self,
        session: Session,
        entity: str,
        *,
        start: datetime,
        end: datetime,
    ) -> int:
        """
        返回某实体在 [start, end) 区间内出现过的独立 raw_source 数量。

        用于计算 cross_source（Req 7.3）。取值范围 0~3（twitter / binance_square / discord）。
        """
        stmt = (
            select(func.count(func.distinct(EntityMention.raw_source)))
            .where(
                EntityMention.entity == entity,
                EntityMention.ts >= start,
                EntityMention.ts < end,
            )
        )
        return int(session.scalar(stmt) or 0)

    # -------------------------------------------------------------------
    # 流式读（供 SlidingCounter 回填用）
    # -------------------------------------------------------------------

    def stream_mentions_since(
        self,
        session: Session,
        since: datetime,
        chunk_size: int = 10000,
    ) -> Iterator[tuple[str, datetime]]:
        """
        流式返回 ts >= since 的 (entity, ts) 元组，按 ts 升序。

        用 SQLAlchemy 的 `yield_per` + `.partitions` 避免一次性载入大量记录，
        Req 6.5 的 7 天回填在百万条级别下也能稳定走通。

        每次 `yield` 一条元组；调用方按需做 `break` 中断（见 SlidingCounter.backfill）。
        """
        stmt = (
            select(EntityMention.entity, EntityMention.ts)
            .where(EntityMention.ts >= since)
            .order_by(EntityMention.ts.asc())
            .execution_options(yield_per=chunk_size)
        )
        for row in session.execute(stmt):
            yield (str(row[0]), row[1])
