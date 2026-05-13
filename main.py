from __future__ import annotations

"""
程序入口。

本服务只负责"从已有原始表里读数据 → 经 Ollama 做两级摘要 → 写回摘要表"。
建表/抓取/HTTP 接入等职责都已拆到其他服务里,本服务对数据库只做读写,**不再负责建表**。

启动流程:
- 读取配置(config/settings.py)
- 初始化日志(控制台 + 文件按天滚动)
- 初始化 DB(SQLAlchemy Engine + Session)
- 初始化 LLM 客户端 / 各仓储 / 各 service
- 启动定时任务并常驻运行
"""

import sys
import time
from pathlib import Path

from loguru import logger

from config.settings import get_settings
from db.connection import Database
from db.repositories.binance_repo import BinanceRepo
from db.repositories.discord_repo import DiscordRepo
from db.repositories.entity_mentions_repo import EntityMentionsRepo
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from db.repositories.level1_repo import Level1Repo
from db.repositories.level2_repo import Level2Repo
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from db.repositories.twitter_repo import TwitterRepo
from dictionaries import get_dictionaries
from llm.ollama_client import OllamaClient
from scheduler.jobs import Jobs
from services.l0_dedup import Deduplicator
from services.l0_normalizer import NormalizerService
from services.l1_entity_extractor import EntityExtractor
from services.l2_hotness import HotnessService
from services.l2_sliding_counter import SlidingCounter
from services.level1_service import Level1Service
from services.level2_service import Level2Service


def _init_logging(log_path: str, retention_days: int) -> None:
    """
    初始化日志输出(loguru)。

    - stdout:便于开发/容器查看
    - 文件:按天滚动,保留 retention_days 天
    """
    logger.remove()
    logger.add(
        sys.stdout,
        level="INFO",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
    logger.add(
        log_path,
        level="INFO",
        rotation="00:00",
        retention=f"{retention_days} days",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )


def main() -> None:
    # 读取配置并初始化日志
    settings = get_settings()
    Path(settings.log_path).parent.mkdir(parents=True, exist_ok=True)
    _init_logging(settings.log_path, settings.log_retention_days)

    # 初始化 DB。本服务只读写,不建表:表结构由上游 API/迁移服务保证。
    db = Database(settings)
    logger.info("数据库连接已初始化(不再执行建表)")

    # ======================================================================
    # 老链路（Level1Service / Level2Service）构造
    # ---------------------------------------------------------------------
    # 受 settings.disable_legacy_pipeline 开关控制：
    # - True  → 跳过所有老链路相关初始化（包括 Ollama 客户端），本段空转
    # - False → 老链路和新链路并行跑（原始行为）
    #
    # 关掉老链路后，Jobs.level1_services / level2_services 传空列表，
    # worker 只迭代 Phase 1 新链路的 new_services。
    # ======================================================================
    level1_services: list = []
    level2_services: list = []

    if not settings.disable_legacy_pipeline:
        # 一次摘要(level1)与二次摘要(level2)使用各自的 Ollama 客户端,
        # 这样可以给低频高质量的 level2 配置更大模型/更长 timeout。
        ollama_l1 = OllamaClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model_level1,
            timeout_seconds=settings.ollama_timeout_level1,
        )
        ollama_l2 = OllamaClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model_level2,
            timeout_seconds=settings.ollama_timeout_level2,
        )
        logger.info(
            "Ollama 客户端就绪:level1={} (timeout {}s) / level2={} (timeout {}s)",
            settings.ollama_model_level1,
            settings.ollama_timeout_level1,
            settings.ollama_model_level2,
            settings.ollama_timeout_level2,
        )

        # 仓储层:原始表(twitter/binance/discord)与摘要表(level1/level2)
        twitter_repo = TwitterRepo()
        binance_repo = BinanceRepo()
        discord_repo = DiscordRepo()
        level1_repo = Level1Repo()
        level2_repo = Level2Repo()

        # Prompt 模板目录
        base_dir = Path(__file__).resolve().parent
        prompts_dir = base_dir / "prompts"

        # 业务层:分别为三个 source 构建 service(互不合并)
        level1_services = [
            Level1Service(
                db=db,
                source="twitter",
                batch_size=settings.batch_size,
                raw_repo=twitter_repo,
                level1_repo=level1_repo,
                ollama=ollama_l1,
                prompt_path=prompts_dir / "level1_twitter.txt",
                timezone=settings.timezone,
            ),
            Level1Service(
                db=db,
                source="binance_square",
                batch_size=settings.batch_size,
                raw_repo=binance_repo,
                level1_repo=level1_repo,
                ollama=ollama_l1,
                prompt_path=prompts_dir / "level1_binance.txt",
                timezone=settings.timezone,
            ),
            Level1Service(
                db=db,
                source="discord",
                batch_size=settings.batch_size,
                raw_repo=discord_repo,
                level1_repo=level1_repo,
                ollama=ollama_l1,
                prompt_path=prompts_dir / "level1_discord.txt",
                timezone=settings.timezone,
            ),
        ]

        level2_services = [
            Level2Service(
                db=db,
                source="twitter",
                threshold=settings.level2_threshold,
                level1_repo=level1_repo,
                level2_repo=level2_repo,
                ollama=ollama_l2,
                prompt_path=prompts_dir / "level2_twitter.txt",
                timezone=settings.timezone,
            ),
            Level2Service(
                db=db,
                source="binance_square",
                threshold=settings.level2_threshold,
                level1_repo=level1_repo,
                level2_repo=level2_repo,
                ollama=ollama_l2,
                prompt_path=prompts_dir / "level2_binance.txt",
                timezone=settings.timezone,
            ),
            Level2Service(
                db=db,
                source="discord",
                threshold=settings.level2_threshold,
                level1_repo=level1_repo,
                level2_repo=level2_repo,
                ollama=ollama_l2,
                prompt_path=prompts_dir / "level2_discord.txt",
                timezone=settings.timezone,
            ),
        ]
    else:
        logger.info(
            "老链路已关闭（settings.disable_legacy_pipeline=True）："
            "跳过 Ollama 客户端与 Level1/Level2 Service 初始化"
        )

    # ======================================================================
    # Phase 1 新链路（crypto-narrative-radar）初始化
    # ---------------------------------------------------------------------
    # 按 design.md §3.8.3 的 7 步顺序执行。老链路的 3 个 level1 / 3 个 level2
    # service 保持不动（上面已构造好），本段只负责构造新 service 并注入 Jobs。
    # 严格约束：新链路绝不调 Ollama，只走 DB / 内存；失败不阻止服务启动。
    # ======================================================================

    # Step 1：加载词典（失败直接阻止启动，避免 prefilter 后续调用时反复 raise）
    # get_dictionaries() 内部用 lru_cache，第一次调用触发加载，后续复用
    try:
        dicts = get_dictionaries()
        logger.info(
            "词典就绪：tickers={} chains={} narratives={} kols={} aliases={}",
            len(dicts.tickers),
            len(dicts.chains),
            len(dicts.narratives),
            len(dicts.kols),
            len(dicts.alias_index),
        )
    except Exception as e:
        logger.error("词典加载失败，服务启动中止：{}", e)
        raise

    # Step 2：构造 3 个新 repo（无状态，可共享）
    normalized_repo = NormalizedMessagesRepo()
    mentions_repo = EntityMentionsRepo()
    hotness_repo = HotnessSnapshotsRepo()

    # Step 3：构造 SlidingCounter 并回填最近 7 天数据
    # handoff.md §3 硬约束：EntityExtractor 和 HotnessService 必须持有同一实例
    sliding_counter = SlidingCounter()
    sc_ok, sc_total, sc_elapsed = sliding_counter.backfill_from_db(
        db,
        max_seconds=settings.sliding_counter_backfill_max_seconds,
        warn_seconds=settings.sliding_counter_backfill_warn_seconds,
    )
    logger.info(
        "SlidingCounter backfill 结束：ok={} total={} elapsed={:.1f}s",
        sc_ok,
        sc_total,
        sc_elapsed,
    )

    # Step 4：构造 Deduplicator 并回填最近 window_hours 小时指纹
    dedup = Deduplicator(
        hamming_threshold=settings.dedup_hamming_threshold,
        window_hours=settings.dedup_window_hours,
    )
    dedup_ok = dedup.backfill_from_db(db)
    if not dedup_ok:
        logger.error("Deduplicator backfill 失败，首批消息可能漏判重（DB UNIQUE 会兜底）")

    # Step 5a：Normalizer（三源 → normalized_messages + SimHash 判重）
    normalizer_service = NormalizerService(
        db=db,
        normalized_repo=normalized_repo,
        dedup=dedup,
        batch_size=settings.normalizer_batch_size,
        timezone=settings.timezone,
    )

    # Step 5b：EntityExtractor（normalized_messages → entity_mentions + 同步 add 到 sliding_counter）
    # ★ sliding_counter 必须与 HotnessService 共用同一实例
    entity_extractor = EntityExtractor(
        db=db,
        normalized_repo=normalized_repo,
        mentions_repo=mentions_repo,
        sliding_counter=sliding_counter,
        batch_size=settings.entity_extractor_batch_size,
    )

    # Step 5c：HotnessService 多实例（Phase 2.1 多窗口热度排行榜）
    # ---------------------------------------------------------------------
    # 三个实例 [1h, 6h, 24h] 共享同一个 sliding_counter / mentions_repo /
    # hotness_repo 引用——关键不变量，否则短窗计数对不上。
    # 1h 必需（构造失败应阻塞启动）；6h / 24h 可降级（构造失败只 log.error）。
    # ---------------------------------------------------------------------

    # Step 5c.1：1h 实例（必需，沿用 Phase 1 行为）
    hotness_1h = HotnessService(
        db=db,
        mentions_repo=mentions_repo,
        hotness_repo=hotness_repo,
        sliding_counter=sliding_counter,
        window_type="1h",
        top_k=settings.hotness_top_k,
        smoothing=settings.hotness_smoothing,
        short_hours=settings.hotness_short_hours,
        baseline_days=settings.hotness_baseline_days,
        min_baseline_count=settings.hotness_min_baseline_count,
        timezone=settings.timezone,
        exclude_entities=settings.hotness_exclude_entities,
    )
    hotness_services: list[HotnessService] = [hotness_1h]
    logger.info(
        "HotnessService(1h) 启动：top_k={} smoothing={} baseline_days={} "
        "min_baseline_count={}",
        settings.hotness_top_k,
        settings.hotness_smoothing,
        settings.hotness_baseline_days,
        settings.hotness_min_baseline_count,
    )

    # Step 5c.2：6h 实例（可选，构造失败只 log.error 不阻塞启动）
    if settings.hotness_6h_enabled:
        try:
            hotness_6h = HotnessService(
                db=db,
                mentions_repo=mentions_repo,
                hotness_repo=hotness_repo,
                sliding_counter=sliding_counter,
                window_type="6h",
                top_k=settings.hotness_6h_top_k,
                smoothing=settings.hotness_6h_smoothing,
                short_hours=6,
                baseline_days=settings.hotness_6h_baseline_days,
                min_baseline_count=settings.hotness_6h_min_baseline_count,
                timezone=settings.timezone,
                exclude_entities=settings.hotness_6h_exclude_entities,
            )
            hotness_services.append(hotness_6h)
            logger.info(
                "HotnessService(6h) 启动：top_k={} smoothing={} baseline_days={} "
                "min_baseline_count={}",
                settings.hotness_6h_top_k,
                settings.hotness_6h_smoothing,
                settings.hotness_6h_baseline_days,
                settings.hotness_6h_min_baseline_count,
            )
        except ValueError as e:
            # __post_init__ 校验失败（baseline 数学约束 / window_type 拼错）
            # 不阻塞启动，1h 实例继续工作
            logger.error("HotnessService(6h) 构造失败已跳过：{}", e)
    else:
        logger.info("HotnessService(6h) 未启用（hotness_6h_enabled=False）")

    # Step 5c.3：24h 实例（可选）
    if settings.hotness_24h_enabled:
        try:
            hotness_24h = HotnessService(
                db=db,
                mentions_repo=mentions_repo,
                hotness_repo=hotness_repo,
                sliding_counter=sliding_counter,
                window_type="24h",
                top_k=settings.hotness_24h_top_k,
                smoothing=settings.hotness_24h_smoothing,
                short_hours=24,
                baseline_days=settings.hotness_24h_baseline_days,
                min_baseline_count=settings.hotness_24h_min_baseline_count,
                timezone=settings.timezone,
                exclude_entities=settings.hotness_24h_exclude_entities,
            )
            hotness_services.append(hotness_24h)
            logger.info(
                "HotnessService(24h) 启动：top_k={} smoothing={} baseline_days={} "
                "min_baseline_count={}",
                settings.hotness_24h_top_k,
                settings.hotness_24h_smoothing,
                settings.hotness_24h_baseline_days,
                settings.hotness_24h_min_baseline_count,
            )
        except ValueError as e:
            logger.error("HotnessService(24h) 构造失败已跳过：{}", e)
    else:
        logger.info("HotnessService(24h) 未启用（hotness_24h_enabled=False）")

    # Step 6：根据 SlidingCounter 回填结果决定所有 HotnessService 实例首轮是否敢跑
    # 回填失败时各实例本轮自跳过 + 自动置回 True，给下一轮机会
    for svc in hotness_services:
        svc._counter_ready = sc_ok
    if not sc_ok:
        logger.warning(
            "SlidingCounter backfill 失败，{} 个 HotnessService 实例首轮都会自动跳过",
            len(hotness_services),
        )

    new_services = [normalizer_service, entity_extractor, *hotness_services]

    # =============================================================================
    # Step 5d-pre：CooccurrenceService（Phase 2.5 实体共现网络，design.md §3.7）
    # -----------------------------------------------------------------------------
    # 在 hotness_services 之后构造（共享 mentions_repo / sliding_counter 关键不变量），
    # 但放在 AlertTriggerService（Step 5d）之前——本任务**只产数据不接 Telegram**，
    # 调度顺序对告警逻辑无影响（alert 只读 hotness_snapshots(1h)）。
    #
    # 启用条件：settings.cooccur_enabled=True（默认 True）。任一构造失败被
    # try/except ValueError 兜底（__post_init__ 校验失败），不阻塞启动。
    # =============================================================================
    if settings.cooccur_enabled:
        from db.repositories.cooccurrence_repo import CooccurrenceRepo
        from services.l3_cooccurrence import CooccurrenceService

        try:
            cooccur_service = CooccurrenceService(
                db=db,
                mentions_repo=mentions_repo,
                cooccur_repo=CooccurrenceRepo(),
                sliding_counter=sliding_counter,
                window_type=settings.cooccur_window_type,
                top_pairs=settings.cooccur_top_pairs,
                min_cooccur_count=settings.cooccur_min_cooccur_count,
                min_pmi=settings.cooccur_min_pmi,
                min_window_msgs=settings.cooccur_min_window_msgs,
                timezone=settings.timezone,
            )
            new_services.append(cooccur_service)
            logger.info(
                "CooccurrenceService 启动:window={} top_pairs={} min_pmi={} "
                "min_cooccur={}",
                settings.cooccur_window_type,
                settings.cooccur_top_pairs,
                settings.cooccur_min_pmi,
                settings.cooccur_min_cooccur_count,
            )
        except ValueError as e:
            # __post_init__ 校验失败（window_type 拼错 / min_cooccur_count 非法）
            # 不阻塞启动，hotness/alert 继续工作
            logger.error("CooccurrenceService 构造失败已跳过：{}", e)
    else:
        logger.info("CooccurrenceService 未启用（cooccur_enabled=False）")

    # Step 5d：AlertTriggerService（Phase 2 Task 2.2 — Telegram 实时告警）
    # ---------------------------------------------------------------------
    # 仅当 telegram_bot_token + telegram_chat_id 都非空才构造 Service。
    # 任一为空 → log INFO 跳过（用户决策"先观察 hotness 再决定要不要开告警"，
    # 不 raise 阻塞启动；requirements.md Req 4.3 / Req 5.4 / 硬约束 5）。
    # 调度顺序：必须在 hotness_service 之后（保证最新榜单已写入再扫描）。
    #
    # 预声明 None：Step 5e（RealtimeAlertService）需要引用 alert_service /
    # telegram_client，无论 if 分支是否进入都要保证名字在作用域内。
    alert_service = None
    telegram_client = None
    if settings.telegram_bot_token and settings.telegram_chat_id:
        from notifications.telegram_client import TelegramClient
        from services.l2_alert_trigger import AlertTriggerService

        telegram_client = TelegramClient(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            timeout_seconds=settings.telegram_timeout_seconds,
        )
        # Phase 2.7 Task 6：briefing 集成
        # briefing_enabled 时构造 BriefingsRepo() 注入 alert_service，让告警消息
        # 附带 LLM 简报（narrative / catalyst）。未启用 briefing → 传 None 走原模板。
        # 注意：alert 调度顺序在 briefing 之前，所以本次 worker 拉到的是上一轮
        # （15min 前）的 briefing；首次上榜的 entity 无 briefing 自动降级。
        _alert_briefing_repo = None
        if settings.briefing_enabled:
            from db.repositories.briefings_repo import BriefingsRepo
            _alert_briefing_repo = BriefingsRepo()
        alert_service = AlertTriggerService(
            db=db,
            hotness_repo=hotness_repo,
            telegram_client=telegram_client,
            growth_threshold=settings.alert_growth_threshold,
            min_count_short=settings.alert_min_count_short,
            min_cross_source=settings.alert_min_cross_source,
            cooldown_minutes=settings.alert_cooldown_minutes,
            escalation_growth_multiplier=settings.alert_escalation_growth_multiplier,
            heartbeat_hours=settings.alert_heartbeat_hours,
            message_template=settings.alert_message_template,
            briefing_repo=_alert_briefing_repo,
        )
        new_services.append(alert_service)
        logger.info(
            "AlertTriggerService 启动：growth_threshold={} cooldown={}min "
            "escalation×{} heartbeat={}h briefing={}",
            settings.alert_growth_threshold,
            settings.alert_cooldown_minutes,
            settings.alert_escalation_growth_multiplier,
            settings.alert_heartbeat_hours,
            "ON" if _alert_briefing_repo is not None else "OFF",
        )
    else:
        logger.info("Telegram 告警未配置（token/chat_id 为空），已禁用")

    # =============================================================================
    # Step 5e：RealtimeAlertService（Phase 2.4 实时触发，design.md §3.5）
    # -----------------------------------------------------------------------------
    # 通过 EntityExtractor.notify(N) hook 同步触发实时榜计算 + Telegram 推送，
    # 把端到端最坏延迟从 14~15min 压到 1~2min。
    #
    # 启用条件（三者全满足才启用）：
    #   1. settings.realtime_enabled = True
    #   2. Telegram 已配置（telegram_bot_token / telegram_chat_id 都非空）
    #   3. AlertTriggerService 已构造（共享 _alert_records 必须有"载体"）
    #
    # 任一条件不满足 → 跳过构造 + 打 INFO 日志说明原因（design Req 7.2 三种场景）
    #
    # ★ 反向注入：构造完后通过 entity_extractor.realtime_trigger = realtime_service
    # 把 hook 挂上去（EntityExtractor 已去 frozen，运行时赋值合法）。
    # ★ 不加入 new_services：本 Service 由 EntityExtractor 内部触发，
    # 不需要 worker 主循环调度。
    # =============================================================================

    realtime_service = None
    if (
        settings.realtime_enabled
        and settings.telegram_bot_token
        and settings.telegram_chat_id
        and alert_service is not None
    ):
        from services.l2_realtime_trigger import RealtimeAlertService

        realtime_service = RealtimeAlertService(
            db=db,
            mentions_repo=mentions_repo,
            # 与 EntityExtractor / HotnessService 共享同一 SlidingCounter 实例
            sliding_counter=sliding_counter,
            # 与 AlertTriggerService 共享同一 TelegramClient 实例
            telegram_client=telegram_client,
            # ★ 共享冷却 dict（同一对象引用，绝不 deepcopy）
            shared_alert_records=alert_service._alert_records,
            # 触发参数（来自 settings）
            burst_threshold=settings.realtime_burst_threshold,
            growth_threshold=settings.realtime_growth_threshold,
            min_count_short=settings.realtime_min_count_short,
            # 公式参数（与整点榜 1h 窗口对齐，复用 hotness_smoothing /
            # hotness_baseline_days；short_hours 在 _trigger_immediate 内固定为 1）
            smoothing=settings.hotness_smoothing,
            baseline_days=settings.hotness_baseline_days,
            baseline_hours_window=settings.hotness_baseline_days * 24
            - settings.hotness_short_hours,
            # 黑名单与整点 1h 榜对齐
            exclude_entities=settings.hotness_exclude_entities,
            # 智能冷却参数（4 路径决策树，与整点完全一致）
            cooldown_minutes=settings.alert_cooldown_minutes,
            escalation_growth_multiplier=settings.alert_escalation_growth_multiplier,
            heartbeat_hours=settings.alert_heartbeat_hours,
            # 消息模板与时区
            message_template=settings.alert_message_template,
            timezone=settings.timezone,
        )

        # ★ 反向注入：让 EntityExtractor.run_once 末尾的 notify hook 拿到 service
        entity_extractor.realtime_trigger = realtime_service

        logger.info(
            "RealtimeAlertService 启动：burst={} growth_threshold={} "
            "min_count_short={} cooldown={}min",
            settings.realtime_burst_threshold,
            settings.realtime_growth_threshold,
            settings.realtime_min_count_short,
            settings.alert_cooldown_minutes,
        )
    else:
        # 三种跳过场景（Req 7.2）
        if not settings.realtime_enabled:
            logger.info("RealtimeAlertService 未启用（realtime_enabled=False）")
        elif not (settings.telegram_bot_token and settings.telegram_chat_id):
            logger.info("RealtimeAlertService 跳过：Telegram 未配置")
        else:
            logger.info("RealtimeAlertService 跳过：AlertTriggerService 未启用")

    # =============================================================================
    # Step 5f：BriefingService（Phase 2.7 LLM 定向简报，design.md §10）
    # -----------------------------------------------------------------------------
    # 每 15 分钟整点对齐取最新 1h 榜 Top-N，给 growth >= min_growth 的实体调
    # OllamaClient 生成 JSON 简报（叙事/催化/资金逻辑/sentiment/confidence），
    # 写入 entity_briefings 表。
    #
    # ★ 调度位置：必须排在所有写库 service（Normalizer / EntityExtractor /
    # 全部 HotnessService / Cooccur / AlertTrigger）**之后**——LLM 推理慢
    # （CPU 模式 ~30s/次，Top-5 一轮 ~2.5 分钟），不能阻塞实时管道。
    #
    # 启用条件：仅 settings.briefing_enabled=True 才构造（OllamaClient 自带
    # 优雅降级，连不上 Ollama 时 chat() 抛 RuntimeError，BriefingService 单 entity
    # try/except 隔离 + log.warning，不影响其它 service）。
    #
    # ★ 这是 Phase 1/2.x"零 LLM"硬约束的明确突破——但只在"信号产生后加解释"，
    # 不让 LLM 反向影响信号产生链路。详见 docs/faq_design_decisions.md Q11。
    # =============================================================================
    if settings.briefing_enabled:
        from db.repositories.briefings_repo import BriefingsRepo
        from db.repositories.cooccurrence_repo import CooccurrenceRepo
        from llm.ollama_client import OllamaClient
        from services.l5_briefing import BriefingService
        from pathlib import Path as _Path

        ollama_l5 = OllamaClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model_level5,
            timeout_seconds=settings.ollama_timeout_level5,
        )
        prompts_dir = _Path(__file__).resolve().parent / "prompts"
        briefing_service = BriefingService(
            db=db,
            hotness_repo=hotness_repo,
            mentions_repo=mentions_repo,
            normalized_repo=normalized_repo,
            briefing_repo=BriefingsRepo(),
            ollama=ollama_l5,
            prompt_path=prompts_dir / "level5_briefing.txt",
            # 共现 hint：如果 cooccur_enabled 已启用就传 repo 让 prompt 带 hint
            cooccur_repo=(
                CooccurrenceRepo() if settings.cooccur_enabled else None
            ),
            top_n=settings.briefing_top_n,
            min_growth=settings.briefing_min_growth,
            evidence_count=settings.briefing_evidence_count,
            timezone=settings.timezone,
        )
        new_services.append(briefing_service)
        logger.info(
            "BriefingService 启动：top_n={} min_growth={} evidence_count={} "
            "model={} cooccur_hint={}",
            settings.briefing_top_n,
            settings.briefing_min_growth,
            settings.briefing_evidence_count,
            settings.ollama_model_level5,
            "ON" if settings.cooccur_enabled else "OFF",
        )
    else:
        logger.info("BriefingService 未启用（briefing_enabled=False）")

    # Step 7：Jobs 构造时注入 new_services
    # 调度层:level1 / level2 / new_services 共用一个 worker 线程串行触发
    jobs = Jobs(
        level1_services=level1_services,
        level2_services=level2_services,
        poll_interval_seconds=settings.poll_interval_seconds,
        new_services=new_services,
    )
    jobs.start()
    if settings.disable_legacy_pipeline:
        logger.info(
            "服务启动成功:worker 只跑新链路 (Phase 1 new services={}，空闲 sleep {}s)",
            len(new_services),
            settings.poll_interval_seconds,
        )
    else:
        logger.info(
            "服务启动成功:worker 串行跑老+新链路 "
            "(level1 batch={} / level2 threshold={} / new services={}，空闲 sleep {}s)",
            settings.batch_size,
            settings.level2_threshold,
            len(new_services),
            settings.poll_interval_seconds,
        )

    try:
        # worker 在后台线程跑,主线程只需要常驻
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("收到退出信号,正在停止调度器...")
    finally:
        jobs.shutdown(wait=False)


if __name__ == "__main__":
    main()
