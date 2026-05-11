from __future__ import annotations

"""
L1 实体抽取服务。

职责（对应 requirements.md Req 4.5~4.8）：
- 从 `normalized_messages` 拉一批 `is_duplicate=FALSE AND l1_processed_at IS NULL` 的原版消息
- 每条调 `prefilter.classify()` 拿到 `decision.entities`
- 判断 author 是否命中 `kols.yaml`（打 `is_kol_mention` 标记，Phase 1 只打标不计分）
- 把实体 UPSERT 到 `entity_mentions`（靠 `UNIQUE(msg_id, entity)` 幂等）
- 同一事务里把消息 `l1_processed_at = NOW()`，下轮不再拉取
- 落库成功后同步 `SlidingCounter.add(entity, ts.timestamp())` 更新内存计数

事务模型（与设计对齐）：
- 先在独立 session 里拉消息（只读），避免长事务占着行锁
- 再在独立 session 里一次性写入 entity_mentions + 更新 l1_processed_at
- 写失败整批回滚，下一轮会再次扫到这批消息重新尝试（所以 classify 必须是幂等纯函数，
  否则可能重复产生不同 entities —— 当前版 classify 就是幂等的，无需担心）
- SlidingCounter 的 add 放在**写库成功之后**，失败路径下不污染内存计数

幂等保证：
- DB 幂等：entity_mentions 的 `UNIQUE(msg_id, entity)` + ON CONFLICT DO NOTHING
- 消息处理幂等：`mark_l1_processed` 只更新 `l1_processed_at IS NULL` 的行，重试安全

零 LLM：本模块绝不 `import llm.ollama_client`（Phase 1 硬约束）。
"""

from dataclasses import dataclass

from loguru import logger

from db.connection import Database
from db.repositories.entity_mentions_repo import EntityMentionsRepo
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from dictionaries import get_dictionaries
from services.l2_sliding_counter import SlidingCounter
from services.prefilter import classify


@dataclass(frozen=True)
class EntityExtractor:
    """
    L1 实体抽取服务（无状态，可单例）。

    构造参数：
    - db：Database 连接池
    - normalized_repo / mentions_repo：无状态 repo 实例
    - sliding_counter：★ 必须是与 HotnessService 共享的同一实例，
      否则短窗计数和排行榜完全对不上（handoff.md §5 强调的关键点）
    - batch_size：每轮处理上限，默认 500（对应 settings.entity_extractor_batch_size）
    """

    db: Database
    normalized_repo: NormalizedMessagesRepo
    mentions_repo: EntityMentionsRepo
    sliding_counter: SlidingCounter
    batch_size: int = 500

    def run_once(self) -> bool:
        """
        执行一轮实体抽取。

        返回值：
        - True：本轮至少处理了一条消息（不管有没有实体落库，mark_l1_processed
          只要动过一行就算"工作"过）
        - False：没有未处理消息，worker 应 sleep 到下一轮
        """
        # 阶段 1：拉消息（独立只读 session）
        try:
            with self.db.get_session() as session:
                msgs = self.normalized_repo.fetch_unprocessed_for_l1(
                    session, limit=self.batch_size
                )
        except Exception as e:
            logger.error("entity_extractor fetch failed: {}", e)
            return False

        if not msgs:
            return False

        # 阶段 2：内存抽取（纯函数，不访问 DB）
        dicts = get_dictionaries()
        to_insert: list[dict] = []
        to_mark_ids: list[int] = []

        for m in msgs:
            decision = classify(m.text)
            # KOL 判定：author 字符串（小写）直接在 dicts.kols 里查
            # 简化版：不拆 Twitter handle 等细节，Phase 2 再做
            is_kol = (m.author or "").lower() in dicts.kols

            for e in decision.entities:
                to_insert.append(
                    {
                        "msg_id": int(m.id),
                        "entity": e.name,
                        "entity_type": e.entity_type,
                        "raw_source": m.raw_source,
                        "ts": m.ts,
                        "confidence": float(e.confidence),
                        "is_kol_mention": is_kol,
                        "engagement": int(m.engagement),
                        "author_weight": float(m.author_weight),
                    }
                )
            # 不论是否抽到实体，都要标记已处理（Req 4.7）
            to_mark_ids.append(int(m.id))

        # 阶段 3：写库（同一事务 UPSERT + mark）
        try:
            with self.db.get_session() as session:
                try:
                    if to_insert:
                        self.mentions_repo.bulk_upsert(session, to_insert)
                    self.normalized_repo.mark_l1_processed(session, to_mark_ids)
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
        except Exception as e:
            logger.error(
                "entity_extractor write failed: {} (msgs={}, entities={})",
                e,
                len(msgs),
                len(to_insert),
            )
            return False

        # 阶段 4：写库成功后才更新 SlidingCounter 内存计数
        # 失败路径下绝不动内存，避免"库里没落，但内存认为落了"的脏状态
        for item in to_insert:
            self.sliding_counter.add(item["entity"], item["ts"].timestamp())

        logger.info(
            "entity_extractor 本轮：处理 {} 条消息 → 产出 {} 条实体提及",
            len(msgs),
            len(to_insert),
        )
        return True


__all__ = ["EntityExtractor"]
