from __future__ import annotations

"""
L2 热度排行榜服务。

职责（对应 requirements.md Req 7.1~7.10）：
- 每 15 分钟对齐到 :00 / :15 / :30 / :45 触发一次
- 从 SlidingCounter 拿 24h 活跃 entity 作为候选集
- 每个候选算 growth_rate / cross_source / final_score / is_new_entity
- Top-K 按 (final_score 降序, count_short 降序, entity 升序) 三级稳定排序
- UPSERT 到 `hotness_snapshots`，`window_type='1h'`

关键状态字段：
- `_last_window_end`：上次成功处理的 window_end，防止同一整点重复处理
- `_counter_ready`：由 main.py 在 SlidingCounter backfill 成功/失败后注入；
  False 时本轮跳过，同时在内部自动置回 True，给下一轮机会

性能说明：
- 每个候选 entity 查两次 DB（baseline + cross_source）。Phase 1 候选集预计
  在 50~500 之间，单轮耗时 < 1s。数量级进一步上涨时改成一条 `GROUP BY entity`
  聚合查询，留到 Gate 1 观测后决定。

失败回滚（Req 7.9）：
- UPSERT 失败时 rollback 整批 + 不更新 `_last_window_end`，下一轮会重试
- 绝不落半条数据到 `hotness_snapshots`

零 LLM：本模块绝不 `import llm.ollama_client`（Phase 1 硬约束）。
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

from db.connection import Database
from db.repositories.entity_mentions_repo import EntityMentionsRepo
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from services.l2_sliding_counter import SlidingCounter


def align_to_quarter(dt: datetime) -> datetime:
    """
    把 `dt` 向下对齐到最近的 :00 / :15 / :30 / :45 整刻钟。

    例：10:23:45 → 10:15:00；10:45:30 → 10:45:00；10:59:59 → 10:45:00。
    """
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


@dataclass
class HotnessService:
    """
    L2 热度快照生成器。

    ★ 与 EntityExtractor 共享同一 `sliding_counter` 实例，否则短窗口计数对不上。
    这条硬约束在 main.py 注入处有显式注释（design.md §3.8.3）。

    默认值与 tasks.md Task 8.2 规划的 Settings 字段保持一致：
    - top_k=20 / smoothing=2.0 / short_hours=1 / baseline_days=7 / min_baseline_count=100
    """

    db: Database
    mentions_repo: EntityMentionsRepo
    hotness_repo: HotnessSnapshotsRepo
    sliding_counter: SlidingCounter

    top_k: int = 20
    smoothing: float = 2.0
    short_hours: int = 1
    baseline_days: int = 7
    min_baseline_count: int = 100
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))

    # --- 运行时状态（mutable，所以 dataclass 不加 frozen）---
    _last_window_end: Optional[datetime] = None
    # main.py 在 SlidingCounter backfill 后注入：True=可跑，False=本轮跳过
    _counter_ready: bool = True

    # -----------------------------------------------------------------
    # 公共 API
    # -----------------------------------------------------------------

    def run_once(self) -> bool:
        """
        执行一轮 Hotness 排行榜生成。

        返回值：
        - True：本轮成功写入一批排行榜（更新了 `_last_window_end`）
        - False：跳过（counter 未就绪 / 未到整点 / 基线不足 / 写库失败）

        跳过场景：
        1. `_counter_ready=False`：SlidingCounter 还没回填好，这一轮不敢算
           自动把 flag 置回 True，给下一轮机会
        2. 当前 window_end <= `_last_window_end`：同一整点已处理过
        3. 近 baseline_days 天 entity_mentions 总数 < min_baseline_count：
           基线样本太少，growth_rate 全是噪音，降级跳过（Req 7.7）
        4. UPSERT 失败：rollback + 不更新 `_last_window_end`，下一轮重试（Req 7.9）
        """
        # ------ 跳过场景 1：SlidingCounter 未就绪 ------
        if not self._counter_ready:
            logger.info("hotness skipped: sliding counter not ready")
            # 自愈：让下一轮能重新尝试；main.py 侧如果后续又调 backfill
            # 还会把 flag 刷新一次，不冲突
            self._counter_ready = True
            return False

        # ------ 对齐整刻钟 ------
        now = datetime.now(self.timezone)
        window_end = align_to_quarter(now)

        # ------ 跳过场景 2：同一整点重复处理 ------
        if self._last_window_end is not None and window_end <= self._last_window_end:
            return False

        # ------ 跳过场景 3：基线样本不足 ------
        try:
            with self.db.get_session() as session:
                baseline_count = self.mentions_repo.count_since(
                    session, window_end - timedelta(days=self.baseline_days)
                )
        except Exception as e:
            logger.error("hotness baseline count failed: {}", e)
            return False

        if baseline_count < self.min_baseline_count:
            logger.info(
                "hotness skipped: baseline data insufficient (count={} < {})",
                baseline_count,
                self.min_baseline_count,
            )
            return False

        # ------ 计算 ------
        start_t = time.time()
        records = self._compute_records(window_end)

        # Req 7.10 三级稳定排序：
        #   1. final_score 降序
        #   2. count_short 降序（同分时活跃度高的靠前）
        #   3. entity 字母序升序（兜底保证跨轮稳定）
        records.sort(
            key=lambda r: (-r["final_score"], -r["count_short"], r["entity"])
        )
        top = records[: self.top_k]

        # ------ UPSERT ------
        try:
            with self.db.get_session() as session:
                try:
                    self.hotness_repo.upsert_batch(
                        session,
                        window_end=window_end,
                        window_type="1h",
                        records=[{**r, "rank": i + 1} for i, r in enumerate(top)],
                    )
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
        except Exception as e:
            # Req 7.9：写失败不更新 _last_window_end，下一轮重试；
            # 本轮已 rollback，hotness_snapshots 不会留脏数据
            logger.error(
                "hotness upsert failed: {} (window_end={}, top={})",
                e,
                window_end,
                len(top),
            )
            return False

        # ------ 结束 ------
        elapsed = time.time() - start_t
        if elapsed > 60:
            logger.warning(
                "hotness run_once 耗时 {:.1f}s（>60s 警告，window_end={}）",
                elapsed,
                window_end,
            )
        logger.info(
            "hotness window_end={} top_k={} elapsed={:.1f}s",
            window_end,
            len(top),
            elapsed,
        )

        self._last_window_end = window_end
        return True

    # -----------------------------------------------------------------
    # 内部辅助
    # -----------------------------------------------------------------

    def _compute_records(self, window_end: datetime) -> list[dict]:
        """
        对 SlidingCounter 的 24h 活跃 entity 逐一算 growth_rate / cross_source / final_score。

        公式（design.md §3.7）：
          baseline_per_hour = count_baseline / (baseline_days * 24 - short_hours)
          growth_rate       = short_count / max(baseline_per_hour, smoothing)
          final_score       = growth_rate * (1 + 0.3 * (cross_source - 1))
          is_new_entity     = (baseline_total == 0 and short_count >= 5)

        短窗没有任何提及（count_short == 0）的 entity 直接跳过，不浪费 DB 查询。
        """
        candidates = self.sliding_counter.active_entities("24h")
        baseline_hours = self.baseline_days * 24 - self.short_hours
        short_start = window_end - timedelta(hours=self.short_hours)
        baseline_start = window_end - timedelta(days=self.baseline_days)

        records: list[dict] = []
        for entity in candidates:
            short_count = self.sliding_counter.count(entity, "1h")
            if short_count == 0:
                continue

            try:
                with self.db.get_session() as session:
                    baseline_total = self.mentions_repo.count_for_entity(
                        session,
                        entity,
                        start=baseline_start,
                        end=short_start,  # 基线期不含短窗，避免双算
                    )
                    cross_source = self.mentions_repo.count_sources_for_entity(
                        session,
                        entity,
                        start=short_start,
                        end=window_end,
                    )
            except Exception as e:
                # 单个 entity 查询失败不拖整批，log warn 后跳过
                logger.warning("hotness entity={} count failed: {}", entity, e)
                continue

            baseline_per_hour = baseline_total / baseline_hours
            growth_rate = short_count / max(baseline_per_hour, self.smoothing)
            final_score = growth_rate * (1 + 0.3 * (cross_source - 1))
            is_new = baseline_total == 0 and short_count >= 5

            records.append(
                {
                    "entity": entity,
                    "count_short": short_count,
                    "count_baseline": baseline_per_hour,
                    "growth_rate": growth_rate,
                    "cross_source": cross_source,
                    "is_new_entity": is_new,
                    "final_score": final_score,
                }
            )

        return records


__all__ = ["HotnessService", "align_to_quarter"]
