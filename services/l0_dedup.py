from __future__ import annotations

"""
L0 去重器（SimHash + 小时分桶内存索引）。

职责（对应 requirements.md Req 2.1~2.6）：
- `compute_simhash(text)`：计算 64 位 SimHash 指纹
- `is_duplicate(sh, now_ts)`：在过去 `window_hours` 小时的内存桶里
  查找汉明距离 ≤ `hamming_threshold` 的任意历史指纹，命中即视为重复
- `add(sh, msg_id, ts)`：把新指纹追加到对应小时桶，并顺手清理超期旧桶
- `backfill_from_db(db)`：进程启动时从 `normalized_messages` 回填最近 24h 的
  指纹（实际小时数 = `window_hours`），让第一批新消息就能被正确判重

为什么用"小时分桶"而不是整个 deque：
- 查询时只需扫当前小时 + 过去 `window_hours` 个桶，O(window_hours × 每桶条数)
- 淘汰时按桶粒度删除，O(1)，避免 deque 逐条 popleft
- Phase 1 数据量估算：三源合计 ~1M 条/天 ≈ 42k 条/小时，24 桶 × 42k ≈ 1M 条
  每条 (simhash, msg_id) ≈ 24 字节，总内存 ~24MB，完全可接受

线程安全：
- Phase 1 下只有 worker 单线程调用 add/is_duplicate（requirements.md Req 8），
  **不加锁**。未来多线程时需要 `threading.Lock` 保护 `_buckets`。
"""

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger
from simhash import Simhash


@dataclass
class Deduplicator:
    """
    SimHash 内存去重器。

    - hamming_threshold：汉明距离阈值（Req 2.2 要求 ≤ 3）
    - window_hours：保留历史的时长（Req 2.3 要求 24h）
    - _buckets：`{hour_bucket_id: deque[(simhash, msg_id)]}`，deque 按插入顺序
    """

    hamming_threshold: int = 3
    window_hours: int = 24

    _buckets: dict[int, deque[tuple[int, int]]] = field(
        default_factory=lambda: defaultdict(deque),
        repr=False,
    )

    # -----------------------------------------------------------------
    # 核心 API
    # -----------------------------------------------------------------

    def compute_simhash(self, text: str) -> int:
        """
        返回 SimHash 指纹，以**有符号 int64** 表示，可直接存入 BIGINT 列。

        simhash 库返回的是 `uint64`（0 ~ 2^64-1），但 PostgreSQL / SQLite 的
        `BIGINT` 是 signed 64-bit（-2^63 ~ 2^63-1），超出范围的 uint64 写入会
        报 DataError（PG）或 `Python int too large` （SQLite）。

        做法：当 uint64 值 ≥ 2^63 时减去 2^64，得到对应的 two's complement 负数。
        - 位模式完全保留，再读回来能还原 uint64 语义
        - 汉明距离通过 `_hamming` 里的 `& 0xFFFFFFFFFFFFFFFF` 掩码兼容有符号值
        - backfill 从 BIGINT 读回的本来就是 signed int64，和这里写入的一一对应，无需额外转换
        """
        value = int(Simhash(text).value)
        if value >= (1 << 63):
            value -= 1 << 64
        return value

    def is_duplicate(self, sh: int, now_ts: float) -> tuple[bool, Optional[int]]:
        """
        在过去 `window_hours` 小时的桶里扫描。

        - `now_ts` 是 Unix 秒（float），内部除以 3600 落桶
        - 扫描范围：`[cur_bucket - window_hours, cur_bucket]`，共 `window_hours + 1` 个桶
          （含当前桶，因为同一小时内的历史消息也要比对）
        - 返回 `(是否重复, 被命中的原版消息 id 或 None)`
        """
        cur_bucket = int(now_ts // 3600)
        start = cur_bucket - self.window_hours
        for h in range(start, cur_bucket + 1):
            bucket = self._buckets.get(h)
            if not bucket:
                continue
            for existing_sh, existing_id in bucket:
                if self._hamming(sh, existing_sh) <= self.hamming_threshold:
                    return True, existing_id
        return False, None

    def add(self, sh: int, msg_id: int, ts: float) -> None:
        """
        把新指纹注册到 `ts` 所属的小时桶。

        顺手触发过期清理，避免桶累积到内存溢出。
        """
        bucket_id = int(ts // 3600)
        self._buckets[bucket_id].append((sh, msg_id))
        self._evict_old(bucket_id)

    # -----------------------------------------------------------------
    # 启动回填
    # -----------------------------------------------------------------

    def backfill_from_db(self, db) -> bool:
        """
        从 `normalized_messages` 回填最近 `window_hours` 小时的 SimHash 索引（Req 2.5）。

        失败时**不抛异常**，只记录 ERROR 并返回 False，由调用方（main.py）
        决定是否继续启动。Phase 1 的约定：即使回填失败，进程也继续起来，
        只是第一批新消息可能漏判重（反正会在 PG 层由 UNIQUE 兜住）。

        返回 True 表示回填成功（条数可能为 0）。
        """
        # 延迟 import：避免模块循环依赖（l0_dedup 是纯算法，不应强依赖 db 层）
        from db.repositories.normalized_messages_repo import NormalizedMessagesRepo

        repo = NormalizedMessagesRepo()
        try:
            with db.get_session() as session:
                rows = repo.fetch_recent_simhashes(session, hours=self.window_hours)
        except Exception as e:
            logger.error("deduplicator backfill failed: {}", e)
            return False

        for row_id, sh, ts in rows:
            self.add(int(sh), int(row_id), ts.timestamp())

        logger.info(
            "deduplicator backfill 完成：回填 {} 条指纹（window_hours={}）",
            len(rows),
            self.window_hours,
        )
        return True

    # -----------------------------------------------------------------
    # 内部辅助
    # -----------------------------------------------------------------

    def _evict_old(self, cur_bucket: int) -> None:
        """
        清理超过 `window_hours` 的旧桶。

        被查询范围是 `[cur_bucket - window_hours, cur_bucket]`，
        所以 `< cur_bucket - window_hours` 的桶都可以扔掉。
        """
        cutoff = cur_bucket - self.window_hours
        for h in list(self._buckets.keys()):
            if h < cutoff:
                del self._buckets[h]

    @staticmethod
    def _hamming(a: int, b: int) -> int:
        """
        两个 64 位整数的汉明距离。

        `compute_simhash` 会把 uint64 转成 signed int64（负数）以兼容 BIGINT；
        此处先做 `& 0xFFFFFFFFFFFFFFFF` 掩码把位模式还原到 uint64，再数 1 bit，
        保证"同一 uint64 值无论是正的还是经过有符号改写后的负数，汉明距离都相等"。
        """
        mask = 0xFFFFFFFFFFFFFFFF
        return bin(((a & mask) ^ (b & mask))).count("1")


__all__ = ["Deduplicator"]
