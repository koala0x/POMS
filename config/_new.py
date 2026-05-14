from __future__ import annotations

"""
新链路业务流水线配置。

涵盖：
- L0 Normalizer / Deduplicator
- L1 EntityExtractor
- L2 SlidingCounter / HotnessService（1h / 6h / 24h 三窗口）
- L3 CooccurrenceService
- L5 BriefingService（业务参数；Ollama 模型 / 超时去 _llm.py）

历史变更（2026-05）：老链路（Level1Service / Level2Service）已淘汰，
旧的 `batch_size` / `level2_threshold` 配置已删。

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

    # ==========================================================================
    # L2 Hotness · 6h 中期窗口（Phase 2.1 多窗口热度排行榜，新增）
    # --------------------------------------------------------------------------
    # 6h 窗口给"半天级中期趋势"做信号源——比 1h 噪音小、比 24h 响应快。
    # 默认与 1h 同款黑名单（屏蔽 BTC/ETH/USDT 等常驻巨头）。
    # ==========================================================================

    # 是否启用 6h 窗口实例。False → main.py 跳过该实例构造，零运行时开销
    hotness_6h_enabled: bool = True

    # Top-K 大小（与 1h 独立配置）
    hotness_6h_top_k: int = 20

    # growth_rate 分母平滑值
    # 6h 窗口噪音比 1h 小（短窗时长 ×6），smoothing 等比放大避免冷启动 growth 虚高
    hotness_6h_smoothing: float = 5.0

    # 基线窗长度（天）。6h 默认沿用 7 天（baseline_hours = 7*24-6 = 162 > 0 OK）
    hotness_6h_baseline_days: int = 7

    # 基线样本充足性门槛
    # 6h 窗口在 7 天 baseline 中需要的样本量约为 1h 的 2 倍（短窗变长 6 倍，
    # 但 baseline 时长基本不变，所需 baseline 样本主要看 baseline 期长度）
    hotness_6h_min_baseline_count: int = 200

    # 6h 黑名单（默认与 1h 相同；可独立调整）
    hotness_6h_exclude_entities: tuple[str, ...] = (
        "BTC", "ETH", "SOL", "BNB",
        "USDT", "USDC", "DAI", "OP", "UNI", "Solana"
    )

    # ==========================================================================
    # L2 Hotness · 3h 中短期窗口（Phase 2.8 新增）
    # --------------------------------------------------------------------------
    # 3h 介于 1h（短期突变）和 6h（半天趋势）之间，适合"持续 2~3 小时的热点"。
    # 比如某 KOL 推文带火了一个新 meme 1 小时后还在被讨论：
    #   - 1h 窗口此时已开始衰减
    #   - 6h 窗口提及还没攒够（要等更久）
    #   - 3h 窗口正好稳定捕捉这段"中短期热度"
    # 默认黑名单与 6h 完全一致（同款常驻巨头屏蔽集合）。
    # ==========================================================================

    # 是否启用 3h 窗口实例
    hotness_3h_enabled: bool = True

    # Top-K 大小
    hotness_3h_top_k: int = 20

    # growth_rate 分母平滑值。3h 噪音介于 1h 和 6h 之间，smoothing 取中间值。
    # 1h smoothing=2.0 / 6h smoothing=5.0 → 3h smoothing=3.0
    hotness_3h_smoothing: float = 3.0

    # 基线窗长度（天）。3h 沿用 7 天（baseline_hours = 7*24-3 = 165 > 0 OK）
    hotness_3h_baseline_days: int = 7

    # 基线样本充足性门槛
    # 3h 窗口在 7 天 baseline 中需要的样本量约介于 1h 与 6h 之间
    hotness_3h_min_baseline_count: int = 150

    # 3h 黑名单（默认与 6h 同款；OP/UNI/Solana 都吵 → 一并屏蔽）
    hotness_3h_exclude_entities: tuple[str, ...] = (
        "BTC", "ETH", "SOL", "BNB",
        "USDT", "USDC", "DAI", "OP", "UNI", "Solana"
    )

    # ==========================================================================
    # L2 Hotness · 24h 长期窗口（Phase 2.1 多窗口热度排行榜，新增)
    # --------------------------------------------------------------------------
    # 24h 窗口给"全天级宏观信号"做信号源——BTC 破新高 / 跌破支撑这种宏观事件
    # 在 1h 维度看不出来（BTC 永远在被聊），但 24h 维度的提及量翻 5~10 倍是真信号。
    # 默认黑名单**只屏蔽稳定币**，保留 BTC/ETH 进榜。
    # ==========================================================================

    # 是否启用 24h 窗口实例
    hotness_24h_enabled: bool = True

    # Top-K 大小
    hotness_24h_top_k: int = 20

    # 24h 窗口的 smoothing：信号最稳定，分母平滑值最大，避免冷启动期 growth 爆炸
    hotness_24h_smoothing: float = 10.0

    # ★ 重要边界：必须 ≥ 8（baseline_days*24 - short_hours > 0）
    # baseline_days=8 时基线小时数 = 8*24-24 = 168 = 7 天纯基线
    # 与 1h 窗口的"7 天基线"语义对齐
    # 设小于 8 → HotnessService.__post_init__ raise ValueError，main.py 兜底降级
    hotness_24h_baseline_days: int = 8

    # 基线样本充足性门槛（更长窗口需要更多样本才稳定）
    # 用户决策：接受冷启动期 24h 榜空 8~12 小时
    hotness_24h_min_baseline_count: int = 500

    # 24h 黑名单：默认**不屏蔽** BTC/ETH/SOL/BNB——24h 维度它们的 growth 突变是真信号
    # 仅屏蔽稳定币（USDT/USDC/DAI 在任何窗口都不该上榜）
    hotness_24h_exclude_entities: tuple[str, ...] = (
        "USDT", "USDC", "DAI", "OP", "UNI", "Solana"
    )

    # ==========================================================================
    # L3 Cooccurrence Network（Phase 2.5 实体共现网络，新增）
    # --------------------------------------------------------------------------
    # 在 entity_mentions 上做实体两两共现统计，用 PMI（Pointwise Mutual Information）
    # 衡量"是不是不寻常的一起出现"，写入新表 entity_cooccurrence。
    #
    # 本任务**只产数据，不接 Telegram**——告警通道留 Phase 2.5.1 单独做，
    # 避免和 AlertTriggerService 在用户视角下混淆（详见 design.md §3.8）。
    # ==========================================================================

    # 是否启用 L3 共现网络。False → main.py 跳过 service 构造，零运行时开销
    cooccur_enabled: bool = True

    # 共现统计窗口：1h 共现噪音太大不实用（窗口内消息少导致随机共现频繁），
    # 24h 才是稳定信号源（design.md §9 决策表）
    cooccur_window_type: str = "24h"

    # 每窗口写 Top-K pair（按 PMI 降序）
    # 100 是经验值——足够覆盖主流叙事的常见组合 + Top-10 的"突然成对"信号
    cooccur_top_pairs: int = 100

    # 短窗共现下限：cooccur_count >= 此值才进入 PMI 评估
    # 共现 1~2 次属偶然，3 次起算趋势（design.md §3.4 经验值）
    cooccur_min_cooccur_count: int = 3

    # PMI 下限：≥ 此值才写库
    # PMI=1.0 ≈ "共现概率是独立预期的 e≈2.7 倍"，是 surface 新叙事的关键信号
    # 部署后建议先观察 1 周 entity_cooccurrence.pmi 的 99% 分位再调整
    cooccur_min_pmi: float = 1.0

    # 窗口消息数下限：< 此值跳过本轮（数据稀疏 PMI 全是噪音）
    # 50 是经验值；当前流量下 24h 内带 ≥1 实体的消息总数 800+，远超阈值
    cooccur_min_window_msgs: int = 50

    # ==========================================================================
    # L5 LLM Briefing（Phase 2.7 LLM 定向简报，新增）
    # --------------------------------------------------------------------------
    # 每 15 分钟整点取最新 1h 榜 Top-N，给 growth >= min_growth 的实体调 LLM
    # 生成 JSON 简报（叙事 / 催化 / 资金逻辑 / sentiment / confidence），
    # 写入新表 entity_briefings。
    #
    # ★ 这是 Phase 1/2.x"零 LLM"硬约束的明确突破——但只在"信号产生后加解释"，
    # 不让 LLM 反向影响信号产生链路。详见 docs/faq_design_decisions.md Q11。
    # ==========================================================================

    # 是否启用 L5 LLM 简报。False → main.py 跳过 service 构造，零开销
    briefing_enabled: bool = True

    # 取最新 1h 榜的 Top-N 实体作为候选（默认 5：CPU 推理慢，单轮 ~2.5 分钟可控）
    # 调大 → 单轮耗时更长，可能拖累 worker 主循环
    # 调小 → 漏掉一些值得 brief 的实体
    # ★ 当前为观察期临时值（10）：让 LLM 给更多实体出简报供质量评估
    # 单轮 ~5 分钟，worker 节奏 30s 轮询能接受
    briefing_top_n: int = 10

    # growth_rate >= 此值才调 LLM；过滤"温和上涨"避免 LLM 浪费在噪音上
    # 30 是经验值；按当前数据流量可能太高（hotness 榜 growth 中位数 ~2），
    # 部署后观察一周再调。
    # ★ 当前为观察期临时值（0.5）：与 alert_growth_threshold=1.0 配套，
    # 让所有上榜实体都尽量带 briefing。观察完毕后改回 5.0
    briefing_min_growth: float = 0.5

    # 每个 entity 喂给 LLM 的代表消息数上限
    # 10 条 × 平均 200 字 ≈ 2000 token，加 prompt 模板 + 输出留白远低于
    # qwen3:8b 的 16384 上下文，安全余量充足
    briefing_evidence_count: int = 10
