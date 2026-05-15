from __future__ import annotations

"""
`hotness_snapshots` 表的仓储。

主要使用方：
- HotnessService：每 15 分钟 UPSERT 一次 Top-K 记录
- 未来的读 API / Streamlit 面板：查询排行榜

Phase 1 仓储只负责写入 + 简单读取。复杂查询（趋势、生命周期）留给 Phase 2/3。
"""

from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.models import HotnessSnapshot


class HotnessSnapshotsRepo:
    """`hotness_snapshots` UPSERT + 查询。"""

    # -------------------------------------------------------------------
    # 写入
    # -------------------------------------------------------------------

    def upsert_batch(
        self,
        session: Session,
        *,
        window_end: datetime,
        window_type: str,
        records: list[dict],
    ) -> int:
        """
        对同一个 `(window_end, window_type)` 的 Top-K 记录做批量 UPSERT。

        `records` 里每个 dict 必须包含：
          - entity / count_short / count_baseline / growth_rate / cross_source
          - is_new_entity / final_score / rank
        可选：entity_type / engagement_sum

        幂等键 `(window_end, window_type, entity)`：
        - Req 7.6 要求 UPSERT 覆盖（已存在时覆盖统计值和 rank，不新增一行）
        - HotnessService 单轮失败后下一轮重试时，上轮写成功的部分也会被覆盖
        """
        if not records:
            return 0

        values = [
            {
                "window_end": window_end,
                "window_type": window_type,
                **rec,
            }
            for rec in records
        ]

        stmt = pg_insert(HotnessSnapshot).values(values)
        update_cols = {
            "entity_type": stmt.excluded.entity_type,
            "count_short": stmt.excluded.count_short,
            "count_baseline": stmt.excluded.count_baseline,
            "growth_rate": stmt.excluded.growth_rate,
            "cross_source": stmt.excluded.cross_source,
            "engagement_sum": stmt.excluded.engagement_sum,
            "is_new_entity": stmt.excluded.is_new_entity,
            "final_score": stmt.excluded.final_score,
            "rank": stmt.excluded.rank,
        }
        stmt = stmt.on_conflict_do_update(
            constraint="uq_hotness_snapshots_window_entity",
            set_=update_cols,
        )
        result = session.execute(stmt)
        return int(result.rowcount or 0)

    # -------------------------------------------------------------------
    # 查询（Phase 1 只给调试用，正式面板 Phase 3 再做）
    # -------------------------------------------------------------------

    def fetch_top_k(
        self,
        session: Session,
        *,
        window_end: datetime,
        window_type: str,
        k: int = 20,
    ) -> list[HotnessSnapshot]:
        """
        取某个时刻的 Top-K 排行榜。

        按 rank 升序返回，供调试、Gate 1 人工巡检、未来 API 展示。
        """
        stmt = (
            select(HotnessSnapshot)
            .where(
                HotnessSnapshot.window_end == window_end,
                HotnessSnapshot.window_type == window_type,
            )
            .order_by(HotnessSnapshot.rank.asc())
            .limit(k)
        )
        return list(session.scalars(stmt).all())

    def fetch_latest_window_end(
        self,
        session: Session,
        window_type: str = "1h",
    ) -> datetime | None:
        """
        返回最近一次已写入的 window_end，供 HotnessService 恢复 _last_window_end 状态。

        如果表为空（刚起服务 / 被清空）返回 None。
        """
        stmt = (
            select(HotnessSnapshot.window_end)
            .where(HotnessSnapshot.window_type == window_type)
            .order_by(HotnessSnapshot.window_end.desc())
            .limit(1)
        )
        return session.scalar(stmt)
