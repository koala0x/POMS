from __future__ import annotations

"""
L0 归一化服务。

职责（对应 requirements.md Req 1.1~1.8）：
- 扫描三源未归一化的原始记录（twitter_posts / binance_square_posts / discord_messages）
- 逐条清洗（strip、跳过空内容）
- 内嵌 Deduplicator 做 SimHash 判重
- 幂等写入 `normalized_messages`（靠 UNIQUE(raw_source, raw_id) + ON CONFLICT DO NOTHING）
- **绝不触碰原始表的 `is_summarized` 字段**（Req 1.8）

扫描策略：
- 三源各自 LEFT JOIN `normalized_messages`，取 `nm.id IS NULL` 的行
- 按 `created_at ASC, id ASC` 排序，保证批次间稳定
- 每源取 `batch_size` 条，合并后逐条处理
- 一轮内有任何源产出新记录就返回 True，让 worker 立即进下一轮

事务模型：
- 每条"SimHash 判重 + insert + dedup.add"在同一个 Session 里处理
- insert 调用方式：`INSERT ... ON CONFLICT DO NOTHING RETURNING id`
  - 返回新 id → 真插入了 → 调 `dedup.add` 把指纹加入内存桶
  - 返回 None → ON CONFLICT 跳过（通常是另一个 worker 或上一轮已处理过） → 不动内存
- 单条异常被吞掉并记日志，不影响其他条（避免一条坏消息卡死整批）

零 LLM：本模块绝不 `import llm.ollama_client`（Phase 1 硬约束）。
"""

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.connection import Database
from db.models import BinanceSquarePost, DiscordMessage, NormalizedMessage, TwitterPost
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from services.l0_dedup import Deduplicator


@dataclass(frozen=True)
class NormalizerService:
    """
    L0 归一化服务（单例）。

    构造参数：
    - db：Database 连接池
    - normalized_repo：NormalizedMessagesRepo 实例（无状态，可共享）
    - dedup：Deduplicator 内存索引（**必须与其他 Phase 1 service 共享同一实例**，
             否则判重会失效 —— 这是 handoff.md v1.2 R3 修订重点）
    - batch_size：每源单轮扫描上限（默认 500，来自 settings.normalizer_batch_size）
    - timezone：业务时区（写入时间字段时使用）
    """

    db: Database
    normalized_repo: NormalizedMessagesRepo
    dedup: Deduplicator
    batch_size: int = 500
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def run_once(self) -> bool:
        """
        执行一轮归一化。

        返回值：
        - True：本轮至少有一条记录成功写入 `normalized_messages`
        - False：三源都没有新数据 / 本轮全部跳过

        worker 用返回值决定 sleep 还是立刻下一轮。
        """
        try:
            with self.db.get_session() as session:
                tw_rows = self._scan_twitter(session)
                bn_rows = self._scan_binance(session)
                dc_rows = self._scan_discord(session)
        except Exception as e:
            logger.error("normalizer scan failed: {}", e)
            return False

        # 合并三源，不做全局排序 —— 因为每源内已按 created_at ASC 排了，
        # 跨源顺序不影响正确性（UNIQUE(raw_source, raw_id) 保证幂等）
        total_scanned = len(tw_rows) + len(bn_rows) + len(dc_rows)
        if total_scanned == 0:
            return False

        inserted = 0
        skipped_empty = 0
        dup_count = 0

        try:
            with self.db.get_session() as session:
                try:
                    for raw_source, raw_id, text, author, ts in tw_rows + bn_rows + dc_rows:
                        cleaned = (text or "").strip()
                        if not cleaned:
                            # Req 1.5：空内容跳过，INFO 日志（修订自 DEBUG）
                            logger.info(
                                "normalizer skip empty content: source={} raw_id={}",
                                raw_source,
                                raw_id,
                            )
                            skipped_empty += 1
                            continue

                        sh = self.dedup.compute_simhash(cleaned)
                        ts_float = ts.timestamp()
                        is_dup, dup_of = self.dedup.is_duplicate(sh, ts_float)

                        new_id = self.normalized_repo.insert(
                            session=session,
                            raw_source=raw_source,
                            raw_id=raw_id,
                            text=cleaned,
                            author=author,
                            ts=ts,
                            engagement=0,
                            author_weight=1.0,
                            simhash=sh,
                            is_duplicate=is_dup,
                            dup_of=dup_of,
                        )
                        if new_id is not None:
                            inserted += 1
                            # 只有真插入了才更新内存桶（避免 ON CONFLICT 跳过时重复加指纹）
                            self.dedup.add(sh, new_id, ts_float)
                            if is_dup:
                                dup_count += 1
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
        except Exception as e:
            logger.error("normalizer write failed: {}", e)
            return False

        logger.info(
            "normalizer 本轮：扫描 {} 条（tw={} bn={} dc={}）→ 写入 {} 条"
            "（重复 {} 条，空内容跳过 {} 条）",
            total_scanned,
            len(tw_rows),
            len(bn_rows),
            len(dc_rows),
            inserted,
            dup_count,
            skipped_empty,
        )
        return inserted > 0

    # -----------------------------------------------------------------
    # 三源扫描（LEFT JOIN normalized_messages 找未归一化的行）
    # -----------------------------------------------------------------

    def _scan_twitter(self, session: Session) -> list[tuple]:
        """
        扫描 twitter_posts 中还没归一化的记录。

        返回 `(raw_source, raw_id, text, author, ts)` 元组列表。
        ts 取 `posted_at`，若为空退化到 `created_at`。
        """
        stmt = (
            select(
                TwitterPost.id,
                TwitterPost.content,
                TwitterPost.author,
                TwitterPost.posted_at,
                TwitterPost.created_at,
            )
            .outerjoin(
                NormalizedMessage,
                (NormalizedMessage.raw_source == "twitter")
                & (NormalizedMessage.raw_id == TwitterPost.id),
            )
            .where(NormalizedMessage.id.is_(None))
            .order_by(TwitterPost.created_at.asc(), TwitterPost.id.asc())
            .limit(self.batch_size)
        )
        rows = session.execute(stmt).all()
        return [
            ("twitter", int(r[0]), r[1], r[2], r[3] or r[4])
            for r in rows
        ]

    def _scan_binance(self, session: Session) -> list[tuple]:
        """扫描 binance_square_posts。规则与 twitter 镜像。"""
        stmt = (
            select(
                BinanceSquarePost.id,
                BinanceSquarePost.content,
                BinanceSquarePost.author,
                BinanceSquarePost.posted_at,
                BinanceSquarePost.created_at,
            )
            .outerjoin(
                NormalizedMessage,
                (NormalizedMessage.raw_source == "binance_square")
                & (NormalizedMessage.raw_id == BinanceSquarePost.id),
            )
            .where(NormalizedMessage.id.is_(None))
            .order_by(BinanceSquarePost.created_at.asc(), BinanceSquarePost.id.asc())
            .limit(self.batch_size)
        )
        rows = session.execute(stmt).all()
        return [
            ("binance_square", int(r[0]), r[1], r[2], r[3] or r[4])
            for r in rows
        ]

    def _scan_discord(self, session: Session) -> list[tuple]:
        """
        扫描 discord_messages。

        Discord 的"author"由 channel_name + username 拼接（与 DiscordMessage.author
        派生属性保持一致），这里直接在 Python 层拼接，不依赖 ORM 的 @property。
        """
        stmt = (
            select(
                DiscordMessage.id,
                DiscordMessage.content,
                DiscordMessage.channel_name,
                DiscordMessage.username,
                DiscordMessage.posted_at,
                DiscordMessage.created_at,
            )
            .outerjoin(
                NormalizedMessage,
                (NormalizedMessage.raw_source == "discord")
                & (NormalizedMessage.raw_id == DiscordMessage.id),
            )
            .where(NormalizedMessage.id.is_(None))
            .order_by(DiscordMessage.created_at.asc(), DiscordMessage.id.asc())
            .limit(self.batch_size)
        )
        rows = session.execute(stmt).all()
        result = []
        for row_id, content, channel, user, posted_at, created_at in rows:
            author = f"#{channel} @{user}"
            result.append(("discord", int(row_id), content, author, posted_at or created_at))
        return result


__all__ = ["NormalizerService"]
