from __future__ import annotations

"""
L3 实体共现网络服务（Phase 2.5 新增）。

职责（对应 requirements.md Req 1~8 / design.md §3）：
- 每 15 分钟对齐到 :00 / :15 / :30 / :45 触发一次（与 HotnessService 同款）
- 候选集 = `sliding_counter.active_entities('24h')`，避免 n² 爆炸
- 24h 窗口内对所有"在同一条消息出现"的实体两两组合做 cooccur_count
- 用 PMI（Pointwise Mutual Information，含简单兜底）算"是不是不寻常的一起出现"
- 7 天 baseline cooccur=0 且当前 cooccur≥3 → `is_new_pair=True`（突然成对）
- 按 PMI 降序取 Top-K，UPSERT 到 `entity_cooccurrence`，`window_type='24h'`

关键状态字段：
- `_last_window_end`：上次成功处理的 window_end，防止同一整点重复处理

性能（design.md §3.5）：
- 当前 4943 行 entity_mentions 规模下，单轮 < 1 秒（Req 4.1）
- 走 SQL 路径：用 mentions_repo 的 SELF JOIN 接口逐 pair 查询 baseline
- 10 万行后切内存方案（design.md §3.5 stub），本任务不实现

失败回滚（与 HotnessService 一致的语义）：
- UPSERT 失败时 rollback + 不更新 `_last_window_end`，下一轮重试
- 单 pair baseline 查询失败只 warning 跳过该 pair，不拖整批

零 LLM：本模块绝不 `import llm.ollama_client`（Phase 1/2.x 硬约束延续）。
不接 Telegram：本任务**只产数据**，告警通道留 Phase 2.5.1（design.md §3.8）。
"""

import itertools
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select

from db.connection import Database
from db.models import EntityMention
from db.repositories.cooccurrence_repo import CooccurrenceRepo
from db.repositories.entity_mentions_repo import EntityMentionsRepo
from services.l2_hotness import align_to_quarter
from services.l2_sliding_counter import SlidingCounter, WINDOWS_SECONDS


# 窗口类型 → 短窗时长（小时），与 HotnessService 同款
_WINDOW_HOURS: dict[str, int] = {
    "1h": 1,
    "6h": 6,
    "24h": 24,
}


def _pmi(cooccur: int, count_a: int, count_b: int, N: int) -> float:
    """
    朴素 PMI（design.md §3.3 起步策略，不带 Laplace 平滑）。

    公式：PMI(a, b) = log( cooccur × N / (count_a × count_b) )

    - PMI = 0：互不相关（独立预期）
    - PMI > 0：正相关（叙事候选）；PMI=1 ≈ 共现概率是独立预期的 e≈2.7 倍
    - PMI < 0：互斥（实际场景几乎不出现）

    边界处理：任一参数 ≤ 0 直接返回 0.0（避免 log(0) / 除零）。
    `min_cooccur_count=3` 已在调用方过滤掉 count=1/2 的极端噪音，
    所以这里用朴素公式即可；观察 1 周后若分布异常再切平滑（迁移成本仅本函数一处）。
    """
    if cooccur <= 0 or count_a <= 0 or count_b <= 0 or N <= 0:
        return 0.0
    expected = (count_a * count_b) / N
    if expected <= 0:
        return 0.0
    return math.log(cooccur / expected)


@dataclass
class CooccurrenceService:
    """
    L3 共现网络快照生成器。

    ★ 与 HotnessService / EntityExtractor 共享同一 `sliding_counter` 实例
    （main.py Step 5e 注入），否则 active_entities 候选集对不上。

    默认值与 NewPipelineSettings 的 `cooccur_*` 字段保持一致；main.py 显式传
    所有参数，单测可省略部分参数走默认。
    """

    db: Database
    mentions_repo: EntityMentionsRepo
    cooccur_repo: CooccurrenceRepo
    sliding_counter: SlidingCounter

    # 共现统计窗口；Req 5.3 默认 24h（共现需要长窗才稳定，1h 噪音太大）
    window_type: str = "24h"

    # 写入参数
    top_pairs: int = 100
    min_cooccur_count: int = 3
    min_pmi: float = 1.0
    min_window_msgs: int = 50

    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))

    # 运行时状态（不持久化；进程重启清零）
    _last_window_end: Optional[datetime] = None

    # ----------------------------------------------------------------------
    # 构造期校验（Req 2.2）
    # ----------------------------------------------------------------------

    def __post_init__(self) -> None:
        """
        两道校验，任一失败即 raise ValueError，错误消息含违规字段名 + 实际值。
        """
        if self.window_type not in _WINDOW_HOURS:
            raise ValueError(
                f"CooccurrenceService window_type={self.window_type!r} 不支持，"
                f"合法值：{sorted(_WINDOW_HOURS.keys())}"
            )
        if self.min_cooccur_count < 1:
            raise ValueError(
                f"CooccurrenceService min_cooccur_count={self.min_cooccur_count} "
                f"必须 >= 1"
            )
        if self.top_pairs < 1:
            raise ValueError(
                f"CooccurrenceService top_pairs={self.top_pairs} 必须 >= 1"
            )

    # ----------------------------------------------------------------------
    # 公共 API
    # ----------------------------------------------------------------------

    def run_once(self) -> bool:
        """
        执行一轮共现快照生成。

        返回值：
        - True：本轮成功写入一批共现 pair
        - False：跳过（同窗口已处理 / 数据稀疏 / 全部不达阈值 / 写库失败）

        跳过场景：
        1. 当前 window_end <= `_last_window_end`：同一整点已处理过
        2. count_distinct_msgs_since < min_window_msgs：数据稀疏，PMI 全噪音
        3. 候选 pair 全部不达 min_cooccur_count / min_pmi：写空集，返回 False
        4. UPSERT 失败：rollback + 不更新 `_last_window_end`，下一轮重试
        """
        # ------ 对齐整刻钟 ------
        now = datetime.now(self.timezone)
        window_end = align_to_quarter(now)

        # ------ 跳过 1：同一整点已处理 ------
        if (
            self._last_window_end is not None
            and window_end <= self._last_window_end
        ):
            logger.info("cooccur skipped: window unchanged")
            return False

        short_hours = _WINDOW_HOURS[self.window_type]
        short_start = window_end - timedelta(hours=short_hours)
        baseline_start = window_end - timedelta(days=7)

        # ------ 跳过 2：数据稀疏 ------
        try:
            with self.db.get_session() as session:
                window_msgs = self.mentions_repo.count_distinct_msgs_since(
                    session, since=short_start, until=window_end
                )
        except Exception as e:
            logger.error("cooccur count_distinct_msgs failed: {}", e)
            return False

        if window_msgs < self.min_window_msgs:
            logger.info(
                "cooccur skipped: data sparse (window_msgs={} < {})",
                window_msgs,
                self.min_window_msgs,
            )
            return False

        # ------ 计算候选 pair ------
        start_t = time.time()
        try:
            pairs = self._compute_pairs(
                window_end=window_end,
                short_start=short_start,
                window_msgs=window_msgs,
            )
        except Exception as e:
            # 单轮算不出来不阻塞 worker 主循环；下一轮再试
            logger.error("cooccur _compute_pairs failed: {}", e)
            return False

        # ------ 过滤 + 排序 + 取 Top-K ------
        eligible = [
            p for p in pairs
            if p["cooccur_count"] >= self.min_cooccur_count
            and p["pmi"] >= self.min_pmi
        ]
        eligible.sort(key=lambda p: (-p["pmi"], p["entity_a"], p["entity_b"]))
        top = eligible[: self.top_pairs]

        # ------ is_new_pair 检测 ------
        new_pairs = 0
        for p in top:
            try:
                p["is_new_pair"] = self._is_new_pair(
                    p["entity_a"],
                    p["entity_b"],
                    baseline_start=baseline_start,
                    short_start=short_start,
                    cooccur_count=p["cooccur_count"],
                )
                if p["is_new_pair"]:
                    new_pairs += 1
            except Exception as e:
                # 单 pair baseline 查询失败只 warning，置 False 跳过
                logger.warning(
                    "cooccur is_new_pair check failed: a={} b={} err={}",
                    p["entity_a"],
                    p["entity_b"],
                    e,
                )
                p["is_new_pair"] = False

        # ------ UPSERT ------
        if not top:
            # 候选集空 / 全部不达阈值——也标记本窗口已处理，避免本轮反复扫
            elapsed = time.time() - start_t
            logger.info(
                "cooccur window_end={} pairs_written=0 new_pairs=0 elapsed={:.1f}s",
                window_end,
                elapsed,
            )
            self._last_window_end = window_end
            return False

        try:
            with self.db.get_session() as session:
                try:
                    self.cooccur_repo.upsert_batch(
                        session,
                        window_end=window_end,
                        window_type=self.window_type,
                        pairs=top,
                    )
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
        except Exception as e:
            logger.error(
                "cooccur upsert failed: {} (window_end={}, pairs={}) "
                "（不更新 _last_window_end，下一轮重试）",
                e,
                window_end,
                len(top),
            )
            return False

        # ------ 结束 ------
        elapsed = time.time() - start_t
        if elapsed > 1.0:
            # 4943-row baseline 1s 是 Req 4.1 软阈值；超过即提示运维关注
            logger.warning(
                "cooccur run_once 慢速：{:.1f}s（>1s 警告，window_end={}）",
                elapsed,
                window_end,
            )
        logger.info(
            "cooccur window_end={} pairs_written={} new_pairs={} elapsed={:.1f}s",
            window_end,
            len(top),
            new_pairs,
            elapsed,
        )

        self._last_window_end = window_end
        return True

    # ----------------------------------------------------------------------
    # 内部：候选 pair 计算 / is_new_pair
    # ----------------------------------------------------------------------

    def _compute_pairs(
        self,
        *,
        window_end: datetime,
        short_start: datetime,
        window_msgs: int,
    ) -> list[dict]:
        """
        从 entity_mentions 拉短窗内的 (msg_id, entity)，按 msg_id 分组生成
        所有两两组合（itertools.combinations，强制 entity_a < entity_b 字典序），
        累计每对的 cooccur_count，并算每个 entity 的独立短窗 count，最后批量
        计算 PMI。

        返回值：
            list[dict]，每个 dict 含
              - entity_a / entity_b（canonical 字典序）
              - cooccur_count
              - pmi

        实现选择：起步走"流式拉数据 + 内存 combinations"路径——
        - 4943 行规模 < 1s（实测）
        - 候选集隐含限制：只统计 active_entities('24h') 内的 entity
          （`active_set` 过滤），避免 n² 爆炸
        - 单条消息内最多取前 10 个实体（max_entities_per_msg=10），
          防止长尾消息组合爆炸（10 个 entity → 45 对，再多上 11 个就 55 对）

        K = 候选实体数（active_entities），仅作为 design.md §3.3 平滑公式
        预留参数；本版用朴素 PMI，K 不参与计算。
        """
        # 候选集 = active_entities('24h')；与 HotnessService 共用同一实例
        # （main.py 注入约束）
        active_entities = set(self.sliding_counter.active_entities("24h"))
        if not active_entities:
            return []

        # 流式拉短窗内的 (msg_id, entity)，按 msg_id 分组
        msg_entities: dict[int, list[str]] = defaultdict(list)
        max_entities_per_msg = 10  # 长尾消息组合爆炸保护（design.md §3.3 风险表）
        with self.db.get_session() as session:
            stmt = (
                select(EntityMention.msg_id, EntityMention.entity)
                .where(
                    EntityMention.ts >= short_start,
                    EntityMention.ts < window_end,
                )
                .order_by(EntityMention.msg_id.asc())
                .execution_options(yield_per=10_000)
            )
            for row in session.execute(stmt):
                msg_id, entity = int(row[0]), str(row[1])
                # 只保留候选集内的实体（避免 n² 爆炸 + 与 HotnessService 候选语义对齐）
                if entity not in active_entities:
                    continue
                bucket = msg_entities[msg_id]
                if len(bucket) < max_entities_per_msg:
                    bucket.append(entity)

        # 累计 cooccur_count + 每个 entity 的短窗 count
        pair_count: Counter[tuple[str, str]] = Counter()
        entity_count: Counter[str] = Counter()
        for entities in msg_entities.values():
            unique = sorted(set(entities))
            if len(unique) < 1:
                continue
            for e in unique:
                entity_count[e] += 1
            if len(unique) < 2:
                continue
            for a, b in itertools.combinations(unique, 2):
                # canonical 字典序：sorted 已保证 a < b
                pair_count[(a, b)] += 1

        # 计算 PMI 并组装返回值
        # N = 短窗内带至少一个实体的消息总数（PMI 公式分母用）
        # 注意：用我们采到的 msg_entities 长度更稳——它已等于 window_msgs 候选侧的
        # 投影；用 mentions_repo 传进来的 window_msgs 兜底（理论上两者相同）
        N = max(window_msgs, 1)

        result: list[dict] = []
        for (entity_a, entity_b), cooccur in pair_count.items():
            count_a = entity_count.get(entity_a, 0)
            count_b = entity_count.get(entity_b, 0)
            pmi = _pmi(cooccur, count_a, count_b, N)
            result.append(
                {
                    "entity_a": entity_a,
                    "entity_b": entity_b,
                    "cooccur_count": int(cooccur),
                    "pmi": float(pmi),
                }
            )
        return result

    def _is_new_pair(
        self,
        entity_a: str,
        entity_b: str,
        *,
        baseline_start: datetime,
        short_start: datetime,
        cooccur_count: int,
    ) -> bool:
        """
        判定一对实体是否"突然成对"（Req 3.3）。

        规则（两条同时成立才 True）：
        1. baseline 期 [baseline_start, short_start) 共现次数 == 0
        2. 当前短窗 cooccur_count >= 3

        短路：cooccur_count < 3 直接返回 False，避免无谓 DB 查询。
        """
        if cooccur_count < 3:
            return False
        with self.db.get_session() as session:
            baseline_count = self.mentions_repo.count_pair_cooccur(
                session,
                entity_a,
                entity_b,
                start=baseline_start,
                end=short_start,
            )
        return baseline_count == 0


__all__ = ["CooccurrenceService", "_pmi"]
