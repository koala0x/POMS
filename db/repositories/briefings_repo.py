from __future__ import annotations

"""
`entity_briefings` 表的仓储（Phase 2.7 新增）。

主要使用方：
- BriefingService：每 15 分钟整点对齐写一批
- AlertTriggerService（可选 Task 6 集成）：渲染消息时按 entity 查询
- scripts/check_status.py（未来可选）：展示最新窗口的简报清单

幂等模型：与 hotness/cooccur 不同，本表用 **ON CONFLICT DO NOTHING**——
一条窗口快照只生成一次，避免 LLM 输出抖动；同窗口同实体重跑 service 不会
覆盖原有结果（设计意图：briefing 跟 LLM 调用一对一，不重发）。
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.models import EntityBriefing


class BriefingsRepo:
    """`entity_briefings` UPSERT（DO NOTHING） + 查询。"""

    # -------------------------------------------------------------------
    # 写入
    # -------------------------------------------------------------------

    def upsert_one(
        self,
        session: Session,
        *,
        entity: str,
        window_end: datetime,
        fields: dict[str, Any],
    ) -> int:
        """
        写入一条 briefing。

        `fields` 必须包含：
          - narrative / catalyst / fund_logic / sentiment / confidence（可空）
          - evidence_msg_ids: list[int]
          - raw_response: dict（JSONB）

        幂等键 `(entity, window_end)`：冲突时 **DO NOTHING**（不覆盖）。
        rowcount=0 表示已存在；rowcount=1 表示新写入。
        """
        values = {
            "entity": entity,
            "window_end": window_end,
            **fields,
        }
        stmt = pg_insert(EntityBriefing).values(values).on_conflict_do_nothing(
            constraint="uq_entity_briefings_entity_window"
        )
        result = session.execute(stmt)
        return int(result.rowcount or 0)

    # -------------------------------------------------------------------
    # 查询
    # -------------------------------------------------------------------

    def fetch_for_entity(
        self,
        session: Session,
        *,
        entity: str,
        window_end: datetime,
    ) -> Optional[EntityBriefing]:
        """
        给定 entity + window_end 取一条 briefing，没有返回 None。

        BriefingService 用：判断本轮是否已生成（避免重复调 LLM）。
        AlertTriggerService（可选）用：渲染告警消息时拼接 narrative。
        """
        stmt = select(EntityBriefing).where(
            EntityBriefing.entity == entity,
            EntityBriefing.window_end == window_end,
        )
        return session.scalar(stmt)

    def fetch_recent(
        self,
        session: Session,
        *,
        window_end: datetime,
        limit: int = 20,
    ) -> list[EntityBriefing]:
        """
        取某个时刻的最新一批 briefings（同窗口所有实体）。

        给 scripts/check_status.py 未来的简报展示节用。
        """
        stmt = (
            select(EntityBriefing)
            .where(EntityBriefing.window_end == window_end)
            .order_by(EntityBriefing.created_at.desc())
            .limit(limit)
        )
        return list(session.scalars(stmt).all())
