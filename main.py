from __future__ import annotations

"""
程序入口。

新链路全栈（crypto-narrative-radar / Phase 2 全部子任务）的常驻服务。
本服务从已有原始表（twitter_posts / binance_square_posts / discord_messages）
读数据，逐层处理后写入新表（normalized_messages / entity_mentions /
hotness_snapshots / entity_cooccurrence / entity_briefings），并按需推送
Telegram 告警。

历史变更（2026-05）：
  - 老链路（Level1Service / Level2Service / Ollama 摘要）已淘汰
  - 删除：services/level1_service.py / level2_service.py
  - 删除：db/repositories/{twitter,binance,discord,level1,level2}_repo.py
  - 删除：prompts/level1_*.txt / level2_*.txt
  - 删除：config/_legacy.py（重构为 config/_llm.py）
  - 删除：settings.disable_legacy_pipeline 开关
  - 保留：summary_level1 / summary_level2 表的历史数据 + 三张原始表的
          is_summarized 字段（避免破坏外部抓取服务）

启动流程：
  - 读取配置（config/settings.py）
  - 初始化日志（控制台 + 文件按天滚动）
  - 初始化 DB（SQLAlchemy Engine + Session）
  - 加载词典 + 构造各 service / repo
  - 启动 worker 线程并常驻运行
"""

import sys
import time
from pathlib import Path

from loguru import logger

from config.settings import get_settings
from db.connection import Database
from db.repositories.entity_mentions_repo import EntityMentionsRepo
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from dictionaries import get_dictionaries
from scheduler.jobs import Jobs
from services.l0_dedup import Deduplicator
from services.l0_normalizer import NormalizerService
from services.l1_entity_extractor import EntityExtractor
from services.l2_hotness import HotnessService
from services.l2_sliding_counter import SlidingCounter


def _init_logging(log_path: str, retention_days: int) -> None:
    """
    初始化日志输出（loguru）。

    - stdout：便于开发/容器查看
    - 文件：按天滚动，保留 retention_days 天
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

    # 初始化 DB。本服务只读写，不建表：表结构由 alembic 迁移管理。
    db = Database(settings)
    logger.info("数据库连接已初始化")

    # ======================================================================
    # Step 1：加载词典
    # ----------------------------------------------------------------------
    # 失败直接阻止启动，避免 prefilter 后续调用时反复 raise。
    # get_dictionaries() 内部 lru_cache，第一次调用触发加载，后续复用。
    # ======================================================================
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

    # ======================================================================
    # Step 2：构造 repo（无状态，可共享）
    # ======================================================================
    normalized_repo = NormalizedMessagesRepo()
    mentions_repo = EntityMentionsRepo()
    hotness_repo = HotnessSnapshotsRepo()

    # ======================================================================
    # Step 3：SlidingCounter + 7 天历史回填
    # ----------------------------------------------------------------------
    # ★ 关键不变量：EntityExtractor / HotnessService / RealtimeAlertService /
    # CooccurrenceService 都共享同一 SlidingCounter 实例，否则短窗计数对不上。
    # ======================================================================
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

    # ======================================================================
    # Step 4：Deduplicator + 24h SimHash 指纹回填
    # ======================================================================
    dedup = Deduplicator(
        hamming_threshold=settings.dedup_hamming_threshold,
        window_hours=settings.dedup_window_hours,
    )
    dedup_ok = dedup.backfill_from_db(db)
    if not dedup_ok:
        logger.error(
            "Deduplicator backfill 失败，首批消息可能漏判重（DB UNIQUE 会兜底）"
        )

    # ======================================================================
    # Step 5a：NormalizerService（三源 → normalized_messages + SimHash 判重）
    # ======================================================================
    normalizer_service = NormalizerService(
        db=db,
        normalized_repo=normalized_repo,
        dedup=dedup,
        batch_size=settings.normalizer_batch_size,
        timezone=settings.timezone,
    )

    # ======================================================================
    # Step 5b：EntityExtractor
    # ----------------------------------------------------------------------
    # normalized_messages → entity_mentions + 同步 add 到 sliding_counter
    # ★ sliding_counter 必须与 HotnessService 共享同一实例
    # ======================================================================
    entity_extractor = EntityExtractor(
        db=db,
        normalized_repo=normalized_repo,
        mentions_repo=mentions_repo,
        sliding_counter=sliding_counter,
        batch_size=settings.entity_extractor_batch_size,
    )

    # ======================================================================
    # Step 5c：HotnessService 多实例（Phase 2.1 多窗口热度排行榜）
    # ----------------------------------------------------------------------
    # 三个实例 [1h, 6h, 24h] 共享同一个 sliding_counter / mentions_repo /
    # hotness_repo 引用。1h 必需（构造失败应阻塞启动）；6h / 24h 可降级
    # （构造失败只 log.error）。
    # ======================================================================

    # 5c.1：1h 实例（必需）
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

    # 5c.2：6h 实例（可选）
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
            logger.error("HotnessService(6h) 构造失败已跳过：{}", e)
    else:
        logger.info("HotnessService(6h) 未启用（hotness_6h_enabled=False）")

    # 5c.3：24h 实例（可选）
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

    # 根据 SlidingCounter backfill 结果决定 HotnessService 首轮是否敢跑
    # 回填失败时各实例本轮自跳过 + 自动置回 True，给下一轮机会
    for svc in hotness_services:
        svc._counter_ready = sc_ok
    if not sc_ok:
        logger.warning(
            "SlidingCounter backfill 失败，{} 个 HotnessService 实例首轮都会自动跳过",
            len(hotness_services),
        )

    new_services: list[object] = [
        normalizer_service,
        entity_extractor,
        *hotness_services,
    ]

    # ======================================================================
    # Step 5d：CooccurrenceService（Phase 2.5 实体共现网络）
    # ----------------------------------------------------------------------
    # 共享 mentions_repo / sliding_counter；只产数据不接 Telegram。
    # 构造失败被 try/except ValueError 兜底（__post_init__ 校验失败），
    # 不阻塞启动。
    # ======================================================================
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
                "CooccurrenceService 启动：window={} top_pairs={} min_pmi={} "
                "min_cooccur={}",
                settings.cooccur_window_type,
                settings.cooccur_top_pairs,
                settings.cooccur_min_pmi,
                settings.cooccur_min_cooccur_count,
            )
        except ValueError as e:
            logger.error("CooccurrenceService 构造失败已跳过：{}", e)
    else:
        logger.info("CooccurrenceService 未启用（cooccur_enabled=False）")

    # ======================================================================
    # Step 5e：AlertTriggerService（Phase 2.2 — Telegram 整点告警）
    # ----------------------------------------------------------------------
    # 仅当 telegram_bot_token + telegram_chat_id 都非空才构造。
    # 调度顺序：必须在 hotness_services 之后（保证最新榜单已写入再扫描）。
    #
    # Phase 2.8：多窗口告警支持
    # - 1h 实例：alert_growth_threshold（默认 20）
    # - 6h 实例：alert_6h_growth_threshold（默认 5），enabled 受 alert_6h_enabled 控制
    # - 24h 实例：alert_24h_growth_threshold（默认 3），enabled 受 alert_24h_enabled 控制
    # 三个实例**共享同一个 _alert_records dict**，避免同 entity 在不同窗口
    # 重复推送（短期突变上 1h 榜后，6h 榜也会上但被冷却拦下）。
    # ======================================================================
    alert_service = None  # 1h 主实例（保留旧名给 RealtimeAlertService 注入用）
    alert_services: list = []
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
            window_type="1h",
            growth_threshold=settings.alert_growth_threshold,
            min_count_short=settings.alert_min_count_short,
            min_cross_source=settings.alert_min_cross_source,
            cooldown_minutes=settings.alert_cooldown_minutes,
            escalation_growth_multiplier=settings.alert_escalation_growth_multiplier,
            heartbeat_hours=settings.alert_heartbeat_hours,
            growth_delta_pct=settings.alert_growth_delta_pct,
            message_template=settings.alert_message_template,
            display_timezone=settings.timezone,
            briefing_repo=_alert_briefing_repo,
        )
        new_services.append(alert_service)
        alert_services.append(alert_service)
        logger.info(
            "AlertTriggerService(1h) 启动：growth_threshold={} cooldown={}min "
            "escalation×{} delta={:.0%} heartbeat={}h briefing={}",
            settings.alert_growth_threshold,
            settings.alert_cooldown_minutes,
            settings.alert_escalation_growth_multiplier,
            settings.alert_growth_delta_pct,
            settings.alert_heartbeat_hours,
            "ON" if _alert_briefing_repo is not None else "OFF",
        )

        # ★ 6h 窗口告警实例
        # 与 1h 共享 _alert_records dict（避免同 entity 双告警）
        if settings.alert_6h_enabled:
            alert_6h = AlertTriggerService(
                db=db,
                hotness_repo=hotness_repo,
                telegram_client=telegram_client,
                window_type="6h",
                growth_threshold=settings.alert_6h_growth_threshold,
                min_count_short=settings.alert_6h_min_count_short,
                min_cross_source=settings.alert_6h_min_cross_source,
                cooldown_minutes=settings.alert_cooldown_minutes,
                escalation_growth_multiplier=settings.alert_escalation_growth_multiplier,
                heartbeat_hours=settings.alert_heartbeat_hours,
                growth_delta_pct=settings.alert_growth_delta_pct,
                message_template=settings.alert_message_template,
                display_timezone=settings.timezone,
                briefing_repo=_alert_briefing_repo,
            )
            # 共享冷却 dict
            alert_6h._alert_records = alert_service._alert_records
            new_services.append(alert_6h)
            alert_services.append(alert_6h)
            logger.info(
                "AlertTriggerService(6h) 启动：growth_threshold={}",
                settings.alert_6h_growth_threshold,
            )

        # ★ 24h 窗口告警实例
        if settings.alert_24h_enabled:
            alert_24h = AlertTriggerService(
                db=db,
                hotness_repo=hotness_repo,
                telegram_client=telegram_client,
                window_type="24h",
                growth_threshold=settings.alert_24h_growth_threshold,
                min_count_short=settings.alert_24h_min_count_short,
                min_cross_source=settings.alert_24h_min_cross_source,
                cooldown_minutes=settings.alert_cooldown_minutes,
                escalation_growth_multiplier=settings.alert_escalation_growth_multiplier,
                heartbeat_hours=settings.alert_heartbeat_hours,
                growth_delta_pct=settings.alert_growth_delta_pct,
                message_template=settings.alert_message_template,
                display_timezone=settings.timezone,
                briefing_repo=_alert_briefing_repo,
            )
            alert_24h._alert_records = alert_service._alert_records
            new_services.append(alert_24h)
            alert_services.append(alert_24h)
            logger.info(
                "AlertTriggerService(24h) 启动：growth_threshold={}",
                settings.alert_24h_growth_threshold,
            )
    else:
        logger.info("Telegram 告警未配置（token/chat_id 为空），已禁用")

    # ======================================================================
    # Step 5f：RealtimeAlertService（Phase 2.4 实时触发）
    # ----------------------------------------------------------------------
    # 通过 EntityExtractor.notify(N) hook 同步触发实时榜计算 + Telegram 推送，
    # 把端到端最坏延迟从 14~15min 压到 1~2min。
    #
    # 启用条件（三者全满足才启用）：
    #   1. settings.realtime_enabled = True
    #   2. Telegram 已配置（telegram_bot_token / telegram_chat_id 都非空）
    #   3. AlertTriggerService 已构造（共享 _alert_records 必须有"载体"）
    #
    # ★ 反向注入：构造完后通过 entity_extractor.realtime_trigger 把 hook 挂上去。
    # ★ 不加入 new_services：本 Service 由 EntityExtractor 内部触发。
    # ======================================================================
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
            sliding_counter=sliding_counter,
            telegram_client=telegram_client,
            shared_alert_records=alert_service._alert_records,
            burst_threshold=settings.realtime_burst_threshold,
            growth_threshold=settings.realtime_growth_threshold,
            min_count_short=settings.realtime_min_count_short,
            smoothing=settings.hotness_smoothing,
            baseline_days=settings.hotness_baseline_days,
            baseline_hours_window=settings.hotness_baseline_days * 24
            - settings.hotness_short_hours,
            exclude_entities=settings.hotness_exclude_entities,
            cooldown_minutes=settings.alert_cooldown_minutes,
            escalation_growth_multiplier=settings.alert_escalation_growth_multiplier,
            heartbeat_hours=settings.alert_heartbeat_hours,
            growth_delta_pct=settings.alert_growth_delta_pct,
            message_template=settings.alert_message_template,
            timezone=settings.timezone,
        )
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
        if not settings.realtime_enabled:
            logger.info("RealtimeAlertService 未启用（realtime_enabled=False）")
        elif not (settings.telegram_bot_token and settings.telegram_chat_id):
            logger.info("RealtimeAlertService 跳过：Telegram 未配置")
        else:
            logger.info("RealtimeAlertService 跳过：AlertTriggerService 未启用")

    # ======================================================================
    # Step 5g：BriefingService（Phase 2.7 LLM 定向简报）
    # ----------------------------------------------------------------------
    # ★ 调度位置：必须排在所有写库 service 之后——LLM 推理慢
    # （CPU 模式 ~30s/次，Top-5 一轮 ~2.5 分钟），不能阻塞实时管道。
    #
    # 单 entity LLM 失败 try/except 隔离 + log.warning，不影响其它 service。
    #
    # ★ 这是 Phase 1/2.x"零 LLM"硬约束的明确突破——但只在"信号产生后加解释"，
    # 不让 LLM 反向影响信号产生链路。详见 docs/faq_design_decisions.md Q11。
    # ======================================================================
    if settings.briefing_enabled:
        from db.repositories.briefings_repo import BriefingsRepo
        from db.repositories.cooccurrence_repo import CooccurrenceRepo
        from llm.ollama_client import OllamaClient
        from services.l5_briefing import BriefingService

        ollama_l5 = OllamaClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model_level5,
            timeout_seconds=settings.ollama_timeout_level5,
        )
        prompts_dir = Path(__file__).resolve().parent / "prompts"
        briefing_service = BriefingService(
            db=db,
            hotness_repo=hotness_repo,
            mentions_repo=mentions_repo,
            normalized_repo=normalized_repo,
            briefing_repo=BriefingsRepo(),
            ollama=ollama_l5,
            prompt_path=prompts_dir / "level5_briefing.txt",
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

    # ======================================================================
    # Step 5h：DigestPusherService（Phase 2.8 定期热榜 Digest 推送）
    # ----------------------------------------------------------------------
    # ★ 补回老链路（Level1Service / Level2Service）淘汰后缺失的"周期性输出"通道。
    # ★ 与 AlertTriggerService 互补：alert 看突变（事件触发），digest 看全貌
    #   （周期触发，绕过冷却）。用户 Phase 2 反馈"输出太少"的核心解决方案。
    #
    # 调度位置：放在所有 hotness_services / alert / briefing 之后，
    # 保证拉到的是本轮已写入的最新榜单。
    # ★ 启用条件：
    #   1. settings.digest_enabled = True
    #   2. Telegram 已配置（telegram_client is not None，与 alert 同款检查）
    # ======================================================================
    if settings.digest_enabled and telegram_client is not None:
        from services.l2_digest_pusher import DigestPusherService

        digest_service = DigestPusherService(
            db=db,
            hotness_repo=hotness_repo,
            telegram_client=telegram_client,
            window_types=settings.digest_window_types,
            top_n=settings.digest_top_n,
            push_every_quarters=settings.digest_push_every_quarters,
            timezone=settings.timezone,
        )
        new_services.append(digest_service)
        logger.info(
            "DigestPusherService 启动：windows={} top_n={} every={} quarters",
            settings.digest_window_types,
            settings.digest_top_n,
            settings.digest_push_every_quarters,
        )
    else:
        if not settings.digest_enabled:
            logger.info("DigestPusherService 未启用（digest_enabled=False）")
        else:
            logger.info("DigestPusherService 跳过：Telegram 未配置")

    # ======================================================================
    # Step 6：启动 worker
    # ======================================================================
    jobs = Jobs(
        new_services=new_services,
        poll_interval_seconds=settings.poll_interval_seconds,
    )
    jobs.start()
    logger.info(
        "服务启动成功：worker 跑 {} 个 service，空闲 sleep {}s",
        len(new_services),
        settings.poll_interval_seconds,
    )

    try:
        # worker 在后台线程跑，主线程只需要常驻
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("收到退出信号，正在停止调度器...")
    finally:
        jobs.shutdown(wait=False)


if __name__ == "__main__":
    main()
