from __future__ import annotations

"""
`normalized_messages` 表的仓储。

由 NormalizerService / EntityExtractor / Deduplicator 共同使用。

核心能力：
- insert：ON CONFLICT DO NOTHING 幂等写入（Req 1.6, 1.7）
- fetch_unprocessed_for_l1：按 "is_duplicate=FALSE AND l1_processed_at IS NULL"
  取待处理消息，EntityExtractor 主路径
- mark_l1_processed：把 L1 处理完成时间戳写回
- fetch_recent_simhashes：供 Deduplicator 启动时重建 24h 内存索引
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.models import NormalizedMessage


class NormalizedMessagesRepo:
    """`normalized_messages` CRUD（不含 Dedup / Normalizer 业务逻辑）。"""

    # -------------------------------------------------------------------
    # 写入
    # -------------------------------------------------------------------

    def insert(
        self,
        session: Session,
        *,
        raw_source: str,
        raw_id: int,
        text: str,
        author: Optional[str],
        ts: datetime,
        engagement: int = 0,
        author_weight: float = 1.0,
        simhash: Optional[int] = None,
        is_duplicate: bool = False,
        dup_of: Optional[int] = None,
    ) -> Optional[int]:
        """
        写入一条归一化消息。

        用 PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` 做幂等：
        - 首次写入 → 返回新生成的 id
        - `(raw_source, raw_id)` 已存在 → ON CONFLICT 跳过，返回 None

        返回 None 让调用方（NormalizerService）知道"这条已归一化过了，
        不要再调 Deduplicator.add 更新内存桶"。
        """
        stmt = (
            pg_insert(NormalizedMessage)
            .values(
                raw_source=raw_source,
                raw_id=raw_id,
                text=text,
                author=author,
                author_weight=author_weight,
                ts=ts,
                engagement=engagement,
                simhash=simhash,
                is_duplicate=is_duplicate,
                dup_of=dup_of,
            )
            .on_conflict_do_nothing(
                constraint="uq_normalized_messages_source_raw"
            )
            .returning(NormalizedMessage.id)
        )
        result = session.execute(stmt).scalar_one_or_none()
        return int(result) if result is not None else None

    # -------------------------------------------------------------------
    # 读取
    # -------------------------------------------------------------------

    def fetch_unprocessed_for_l1(
        self, session: Session, limit: int
    ) -> list[NormalizedMessage]:
        """
        取 L1 未处理的原版消息。

        - `is_duplicate = FALSE`：不统计重复（Req 2.4）
        - `l1_processed_at IS NULL`：没被 EntityExtractor 处理过
        - 按 ts 升序（早的优先处理）；同 ts 再按 id 升序，保证批次间排序稳定
        """
        stmt = (
            select(NormalizedMessage)
            .where(
                NormalizedMessage.is_duplicate.is_(False),
                NormalizedMessage.l1_processed_at.is_(None),
            )
            .order_by(NormalizedMessage.ts.asc(), NormalizedMessage.id.asc())
            .limit(limit)
        )
        return list(session.scalars(stmt).all())

    def fetch_recent_simhashes(
        self, session: Session, hours: int = 24
    ) -> list[tuple[int, int, datetime]]:
        """
        回填 Deduplicator 内存桶（Req 2.5）。

        只取 `is_duplicate = FALSE`（原版）的 simhash：重复版本没必要再进桶，
        会浪费对比次数。
        返回元组列表 `[(id, simhash, ts), ...]`，按 ts 升序。
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        stmt = (
            select(
                NormalizedMessage.id,
                NormalizedMessage.simhash,
                NormalizedMessage.ts,
            )
            .where(
                NormalizedMessage.ts >= cutoff,
                NormalizedMessage.simhash.is_not(None),
                NormalizedMessage.is_duplicate.is_(False),
            )
            .order_by(NormalizedMessage.ts.asc())
        )
        return [(int(r[0]), int(r[1]), r[2]) for r in session.execute(stmt).all()]

    def fetch_raw_ids_in_source(
        self, session: Session, raw_source: str
    ) -> set[int]:
        """
        返回某个源已归一化过的所有 raw_id 集合。

        供 NormalizerService 扫描原始表时做"LEFT JOIN 不存在"判定：
        与其写 SQL 的 LEFT JOIN，不如一次性 SELECT 回来做内存 set 差集，
        小数据量下更简单。如果以后单次扫描超过数万条，再改成 SQL 层 JOIN。
        """
        stmt = select(NormalizedMessage.raw_id).where(
            NormalizedMessage.raw_source == raw_source
        )
        return {int(r) for (r,) in session.execute(stmt).all()}

    # -------------------------------------------------------------------
    # 更新
    # -------------------------------------------------------------------

    def mark_l1_processed(
        self, session: Session, ids: Sequence[int]
    ) -> int:
        """
        把 `l1_processed_at` 写为当前时间。

        只更新 `l1_processed_at IS NULL` 的行（幂等）。
        返回实际被更新的行数（对一致性校验有帮助）。
        """
        if not ids:
            return 0
        stmt = (
            update(NormalizedMessage)
            .where(
                NormalizedMessage.id.in_(list(ids)),
                NormalizedMessage.l1_processed_at.is_(None),
            )
            .values(l1_processed_at=datetime.now(timezone.utc))
        )
        result = session.execute(stmt)
        return int(result.rowcount or 0)
