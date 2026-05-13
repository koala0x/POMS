from __future__ import annotations

"""
L2 滑动窗口计数器（进程内内存单例）。

职责（对应 requirements.md Req 6 + Phase 2.1 多窗口扩展）：
- 维护五个固定窗口（15min / 1h / 6h / 24h / 7d）下每个 entity 的提及时间戳 deque
- `add(entity, ts)`：把一次提及记入所有五个窗口
- `count(entity, window)`：惰性清理窗口外的旧数据后返回当前计数
- `active_entities(window)`：返回在该窗口内至少被提及过一次的全部实体
  （供 HotnessService 扫描候选集用，避免遍历全部历史 entity）
- `backfill_from_db(db)`：进程启动时从 `entity_mentions` 回填最近 7 天数据
  让第一轮 Hotness 就能计算出有意义的 growth_rate

消费方：
- EntityExtractor.run_once：每写一条 entity_mention 就同步 `add()`
- HotnessService.run_once：调 `active_entities('24h')` + `count(entity, '1h')` 拿短窗计数

线程安全：
- Phase 1 全链路单 worker 线程（requirements.md Req 8），**不加锁**
- 未来拆多线程时需要 threading.Lock 保护 `_store`

零 LLM：本模块绝不 `import llm.ollama_client`（Phase 1 硬约束）。
"""

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select


# 五个固定窗口 —— 与 design.md §3.6 对齐
# 窗口名（15min / 1h / 6h / 24h / 7d）是 count(entity, window) 的合法取值，
# 单位是秒，供 cutoff 计算直接用。
# Phase 2.1 多窗口热度排行榜新增 '6h'：让 HotnessService 能产出 6h 中期榜，
# add() 一次写入会同步追加到所有 5 个窗口的 deque（基于 WINDOWS_SECONDS 迭代）。
WINDOWS_SECONDS: dict[str, int] = {
    "15min": 900,
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
}


@dataclass
class SlidingCounter:
    """
    五窗口滑动计数器。

    `_store` 结构：
      {
        "15min": {entity_name: deque[ts_float, ...]},
        "1h":    {...},
        "24h":   {...},
        "7d":    {...},
      }

    每个 window 内按 entity 维护一个 deque，按 ts 升序追加（调用方保证）。
    惰性清理策略：只在 `count` 被调用时 popleft 过期条目，`add` 时不清理，
    避免每次 add 都遍历 deque。7d 桶即使很少被 count 也不会无限膨胀，
    因为 backfill 本身按 7d 截断，运行时的累积量受输入速率限制。
    """

    _store: dict[str, dict[str, deque[float]]] = field(
        default_factory=lambda: {w: defaultdict(deque) for w in WINDOWS_SECONDS},
        repr=False,
    )

    # -----------------------------------------------------------------
    # 核心 API：add / count / active_entities
    # -----------------------------------------------------------------

    def add(self, entity: str, ts: float) -> None:
        """
        记录一次提及。

        同一个 ts 同时追加到五个窗口的 deque 末尾；不做重复性检查——
        EntityExtractor 写 entity_mention 时已有 UNIQUE(msg_id, entity) 兜底，
        同一 (msg, entity) 在 DB 层不会重复，内存侧也就不会重复 add。
        """
        for w in WINDOWS_SECONDS:
            self._store[w][entity].append(ts)

    def count(self, entity: str, window: str) -> int:
        """
        返回 `entity` 在 `window` 内的提及次数。

        - window 不合法（不在 WINDOWS_SECONDS 的 5 个 key 里）→ raise ValueError
        - 惰性清理：从 deque 左端 popleft 所有 ts < cutoff 的条目
        - 空 entity（从未 add 过）返回 0，不在 `_store` 里留占位项
        """
        if window not in WINDOWS_SECONDS:
            raise ValueError(
                f"unknown window: {window!r}，合法值：{sorted(WINDOWS_SECONDS.keys())}"
            )
        cutoff = time.time() - WINDOWS_SECONDS[window]
        dq = self._store[window].get(entity)
        if not dq:
            return 0
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq)

    def active_entities(self, window: str = "24h") -> list[str]:
        """
        返回在 `window` 内至少被提及过一次的所有 entity 名字列表。

        用 deque 的最后一个元素（最新提及时间）做判断：
          `dq[-1] >= cutoff` 意味着该 entity 在窗口内有活动。
        不做惰性清理（与 count 分工：这里只扫描活跃性，清理留到 count 调用时）。
        """
        if window not in WINDOWS_SECONDS:
            raise ValueError(
                f"unknown window: {window!r}，合法值：{sorted(WINDOWS_SECONDS.keys())}"
            )
        cutoff = time.time() - WINDOWS_SECONDS[window]
        return [
            entity
            for entity, dq in self._store[window].items()
            if dq and dq[-1] >= cutoff
        ]

    # -----------------------------------------------------------------
    # 启动回填（Req 6.5, 6.7）
    # -----------------------------------------------------------------

    def backfill_from_db(
        self,
        db,
        *,
        max_seconds: float = 600.0,
        warn_seconds: float = 120.0,
        chunk_size: int = 10_000,
    ) -> tuple[bool, int, float]:
        """
        从 `entity_mentions` 回填最近 7 天的提及数据，重建五窗口内存索引。

        返回三元组 `(是否成功, 回填条数, 实际耗时秒)`。

        四种结局（Req 6.7 情况 A/B/C/D）：
          A. 耗时 ≤ warn_seconds         → INFO 日志，(True, total, elapsed)
          B. warn_seconds < 耗时 ≤ max_seconds → WARN 日志，(True, total, elapsed)
          C. 耗时 > max_seconds          → ERROR 日志，(False, total, elapsed)
                                             中途强制中止，已回填部分保留
          D. 回填中抛异常                 → ERROR 日志，(False, total, elapsed)

        参数：
          - max_seconds：硬上限，超过即强制中止；默认 600s（Req 6.7）
          - warn_seconds：慢速告警阈值；默认 120s；单测可注入小值验证情况 B
          - chunk_size：流式读批大小，默认 10000（design.md §3.6 建议）
        """
        # 延迟 import，避免服务模块在无 DB 场景下也被强制带上 db 层依赖
        from db.models import EntityMention

        start = time.time()
        total = 0
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=WINDOWS_SECONDS["7d"])

        try:
            with db.get_session() as session:
                stmt = (
                    select(EntityMention.entity, EntityMention.ts)
                    .where(EntityMention.ts >= cutoff)
                    .order_by(EntityMention.ts.asc())
                    .execution_options(yield_per=chunk_size)
                )
                result = session.execute(stmt)
                # `partitions(chunk_size)` 每个 batch 是一个 list[Row]，
                # 按批处理让我们能在每 chunk 检查一次累计耗时，
                # 实现情况 C 的硬超时中止（不用开计时器中断）
                for batch in result.partitions(chunk_size):
                    for entity, ts in batch:
                        self.add(entity, ts.timestamp())
                        total += 1
                    elapsed_now = time.time() - start
                    if elapsed_now > max_seconds:
                        # 情况 C：硬超时
                        logger.error(
                            "sliding-counter backfill failed: 超过 {}s 硬上限，"
                            "已回填 {} 条，强制中止（耗时 {:.1f}s）",
                            max_seconds,
                            total,
                            elapsed_now,
                        )
                        return False, total, elapsed_now

        except Exception as e:
            # 情况 D：数据库/查询异常
            elapsed = time.time() - start
            logger.error(
                "sliding-counter backfill failed: {} (耗时 {:.1f}s，已回填 {} 条)",
                e,
                elapsed,
                total,
            )
            return False, total, elapsed

        elapsed = time.time() - start
        if elapsed > warn_seconds:
            # 情况 B：慢速成功
            logger.warning(
                "sliding-counter backfill 慢速成功：耗时 {:.1f}s，回填 {} 条",
                elapsed,
                total,
            )
        else:
            # 情况 A：正常成功
            logger.info(
                "sliding-counter backfill 完成：耗时 {:.1f}s，回填 {} 条",
                elapsed,
                total,
            )
        return True, total, elapsed


__all__ = ["SlidingCounter", "WINDOWS_SECONDS"]
