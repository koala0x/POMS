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

    # Step 5c：HotnessService（entity_mentions + sliding_counter → hotness_snapshots）
    # ★ 同一 sliding_counter 实例；默认值由 Settings 承接（Task 8.2 新增的 hotness_* 字段）
    hotness_service = HotnessService(
        db=db,
        mentions_repo=mentions_repo,
        hotness_repo=hotness_repo,
        sliding_counter=sliding_counter,
        top_k=settings.hotness_top_k,
        smoothing=settings.hotness_smoothing,
        short_hours=settings.hotness_short_hours,
        baseline_days=settings.hotness_baseline_days,
        min_baseline_count=settings.hotness_min_baseline_count,
        timezone=settings.timezone,
        exclude_entities=settings.hotness_exclude_entities,
    )

    # Step 6：根据 SlidingCounter 回填结果决定 HotnessService 首轮是否敢跑
    # 回填失败时本轮跳过并自动置回 True，给下一轮机会（见 HotnessService.run_once）
    hotness_service._counter_ready = sc_ok
    if not sc_ok:
        logger.warning(
            "SlidingCounter backfill 失败，HotnessService 首轮会自动跳过，下一轮继续"
        )

    new_services = [normalizer_service, entity_extractor, hotness_service]

    # Step 5d：AlertTriggerService（Phase 2 Task 2.2 — Telegram 实时告警）
    # ---------------------------------------------------------------------
    # 仅当 telegram_bot_token + telegram_chat_id 都非空才构造 Service。
    # 任一为空 → log INFO 跳过（用户决策"先观察 hotness 再决定要不要开告警"，
    # 不 raise 阻塞启动；requirements.md Req 4.3 / Req 5.4 / 硬约束 5）。
    # 调度顺序：必须在 hotness_service 之后（保证最新榜单已写入再扫描）。
    if settings.telegram_bot_token and settings.telegram_chat_id:
        from notifications.telegram_client import TelegramClient
        from services.l2_alert_trigger import AlertTriggerService

        telegram_client = TelegramClient(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            timeout_seconds=settings.telegram_timeout_seconds,
        )
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
        )
        new_services.append(alert_service)
        logger.info(
            "AlertTriggerService 启动：growth_threshold={} cooldown={}min "
            "escalation×{} heartbeat={}h",
            settings.alert_growth_threshold,
            settings.alert_cooldown_minutes,
            settings.alert_escalation_growth_multiplier,
            settings.alert_heartbeat_hours,
        )
    else:
        logger.info("Telegram 告警未配置（token/chat_id 为空），已禁用")

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
