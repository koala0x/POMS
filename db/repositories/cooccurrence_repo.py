from __future__ import annotations

"""
`entity_cooccurrence` 表的仓储（Phase 2.5 新增）。

主要使用方：
- CooccurrenceService：每 15 分钟 UPSERT 一次 Top-K pair（按 PMI 降序）
- 未来的 scripts/check_status.py / Phase 2.5.1 共现告警：fetch_top_k_pairs / fetch_neighbors

接口与 HotnessSnapshotsRepo 同款：批量 UPSERT 走 PG 的 ON CONFLICT DO UPDATE，
靠 uq_cooccur_pair_window 实现幂等。
"""

from datetime import datetime
from typing import Sequence  # noqa: F401  保留备用，与 hotness_snapshots_repo 风格一致

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.models import EntityCooccurrence


class CooccurrenceRepo:
    """`entity_cooccurrence` UPSERT + 查询。"""

    # -------------------------------------------------------------------
    # 写入
    # -------------------------------------------------------------------

    def upsert_batch(
        self,
        session: Session,
        *,
        window_end: datetime,
        window_type: str,
        pairs: list[dict],
    ) -> int:
        """
        对同一 `(window_end, window_type)` 的 Top-K pair 做批量 UPSERT。

        `pairs` 里每个 dict 必须包含：
          - entity_a / entity_b（调用方保证 entity_a < entity_b 字典序）
          - cooccur_count
          - pmi
          - is_new_pair

        幂等键 `(entity_a, entity_b, window_end, window_type)`：
        - 同窗口重跑时（比如 _last_window_end 被重置）已存在的行被覆盖，
          rowcount 不会暴涨
        - 失败重试场景：上轮写成功的部分会被本轮值覆盖，无脏数据残留
        """
        if not pairs:
            return 0

        values = [
            {
                "window_end": window_end,
                "window_type": window_type,
                **p,
            }
            for p in pairs
        ]

        stmt = pg_insert(EntityCooccurrence).values(values)
        update_cols = {
            "cooccur_count": stmt.excluded.cooccur_count,
            "pmi": stmt.excluded.pmi,
            "is_new_pair": stmt.excluded.is_new_pair,
        }
        stmt = stmt.on_conflict_do_update(
            constraint="uq_cooccur_pair_window",
            set_=update_cols,
        )
        result = session.execute(stmt)
        return int(result.rowcount or 0)

    # -------------------------------------------------------------------
    # 查询
    # -------------------------------------------------------------------

    def fetch_top_k_pairs(
        self,
        session: Session,
        *,
        window_end: datetime,
        window_type: str,
        k: int = 100,
    ) -> list[EntityCooccurrence]:
        """
        取某个时刻的 Top-K pair（按 PMI 降序）。

        给 scripts/check_status.py §5 / 调试 / 未来 Phase 2.5.1 共现告警用。
        """
        stmt = (
            select(EntityCooccurrence)
            .where(
                EntityCooccurrence.window_end == window_end,
                EntityCooccurrence.window_type == window_type,
            )
            .order_by(EntityCooccurrence.pmi.desc())
            .limit(k)
        )
        return list(session.scalars(stmt).all())

    def fetch_neighbors(
        self,
        session: Session,
        *,
        entity: str,
        window_end: datetime,
        k: int = 10,
    ) -> list[EntityCooccurrence]:
        """
        给某 entity 找 PMI 最高的 k 个邻居。

        entity 可能在 entity_a 或 entity_b 任一侧，所以走 OR 查询；
        DB 侧 `idx_cooccur_entity_a / entity_b` 双索引兜底，避免全表扫描。

        本方法只取最新 window_end 的快照（调用方传具体时刻），不跨窗口聚合——
        跨窗口聚合留给 Phase 2.6（实体聚类）做。
        """
        stmt = (
            select(EntityCooccurrence)
            .where(
                EntityCooccurrence.window_end == window_end,
                or_(
                    EntityCooccurrence.entity_a == entity,
                    EntityCooccurrence.entity_b == entity,
                ),
            )
            .order_by(EntityCooccurrence.pmi.desc())
            .limit(k)
        )
        return list(session.scalars(stmt).all())
