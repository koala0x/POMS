from __future__ import annotations

"""
**新链路**配置（Phase 1 crypto-narrative-radar）。

零 LLM 的实体热度排行榜流水线参数：
- L0 Normalizer / Deduplicator
- L1 EntityExtractor
- L2 SlidingCounter / HotnessService

修改这里的字段不会影响老链路（Level1/Level2Service / Ollama）。

每条字段都标注了对应的 requirements.md Req 编号，便于回溯设计意图。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NewPipelineSettings:
    # ==========================================================================
    # L0 Normalizer
    # ==========================================================================

    # 每轮三源各扫描多少条未归一化记录（Req 1.6）
    # 生产默认 500；batch=500 × 3 源 = 单轮最多 1500 条，内存/耗时可控
    normalizer_batch_size: int = 500

    # ==========================================================================
    # L0 SimHash 去重
    # ==========================================================================

    # SimHash 判重阈值：汉明距离 ≤ threshold 视为重复（Req 2.2）
    # 默认 3 是经验值；调大→更激进去重，调小→更保守
    dedup_hamming_threshold: int = 3

    # 判重历史窗口（Req 2.6）。Deduplicator 的内存桶只保留最近 N 小时，
    # 老于此值的桶被自动清理，防止无限增长
    dedup_window_hours: int = 24

    # ==========================================================================
    # L1 Entity Extractor
    # ==========================================================================

    # 每轮从 normalized_messages 取多少条未处理消息
    entity_extractor_batch_size: int = 500

    # ==========================================================================
    # L2 Hotness
    # ==========================================================================

    # 排行榜 Top-K（Req 7.5）
    hotness_top_k: int = 20

    # growth_rate 分母平滑值（Req 7.2）：growth = short / max(baseline_per_h, smoothing)
    # 避免基线=0 时除零；同时让"新实体但只被提 1 次"不会被夸大成 growth=∞
    hotness_smoothing: float = 2.0

    # 短窗时长（小时）。Phase 1 固定 1h，产出 window_type='1h' 的快照
    hotness_short_hours: int = 1

    # 基线窗长度（天）。Phase 1 用 7 天；调短→对突变更敏感、对长期趋势不敏感
    hotness_baseline_days: int = 7

    # 基线样本充足性门槛（Req 7.7）：近 baseline_days 的 entity_mentions 总数
    # 低于此值直接跳过本轮，避免冷启动期 growth_rate 全是噪音
    hotness_min_baseline_count: int = 100

    # Hotness 输出黑名单：这些 entity 不会出现在 hotness_snapshots 排行榜里
    # （但 entity_mentions 表照常记录，能在 SQL 里查到）。
    #
    # 用途：BTC/ETH/USDT 这种"提到很多但 growth 永远 ≈ 1"的常驻巨头，
    # 留在榜上一是占位、二是模糊新币 alpha 信号。把它们屏蔽掉，
    # Top-20 就全是真正"突然热"的新东西。
    #
    # 哪天某个常驻币真有大新闻（比如 BTC 突破历史新高 + 大量讨论），
    # 它的 hotness 表现仍然能在 entity_mentions 里通过 SQL 查到，
    # 只是不出现在每 15 分钟的快照里。
    #
    # 修改后**重启服务**生效（HotnessService 在构造时读取这个值）。
    # 比较时不区分大小写（HotnessService 内部 .upper() 处理）。
    hotness_exclude_entities: tuple[str, ...] = (
        "BTC", "ETH", "SOL", "BNB",
        "USDT", "USDC", "DAI",
    )

    # ==========================================================================
    # L2 SlidingCounter 启动回填（Req 6.7）
    # ==========================================================================

    # 硬上限：超过这个秒数强制中止回填，避免启动期被过量历史数据卡住（情况 C）
    sliding_counter_backfill_max_seconds: int = 600

    # WARN 阈值：低于此值算"快速成功"（情况 A INFO 日志）
    # 在 [warn, max] 区间算"慢速成功"（情况 B WARN 日志）
    # 单测可注入小值（如 0.1）来验证情况 A/B 分支
    sliding_counter_backfill_warn_seconds: int = 120
