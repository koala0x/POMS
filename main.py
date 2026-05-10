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
from db.repositories.level1_repo import Level1Repo
from db.repositories.level2_repo import Level2Repo
from db.repositories.twitter_repo import TwitterRepo
from llm.ollama_client import OllamaClient
from scheduler.jobs import Jobs
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

    # 一次摘要(level1)与二次摘要(level2)使用各自的 Ollama 客户端,
    # 这样可以给低频高质量的 level2 配置更大模型/更长 timeout。
    ollama_l1 = OllamaClient(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model_level1,
        timeout_seconds=settings.ollama_timeout_level1,
        retry_times=settings.ollama_retry_times,
        retry_delay_seconds=settings.ollama_retry_delay_seconds,
    )
    ollama_l2 = OllamaClient(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model_level2,
        timeout_seconds=settings.ollama_timeout_level2,
        retry_times=settings.ollama_retry_times,
        retry_delay_seconds=settings.ollama_retry_delay_seconds,
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

    # 调度层:level1 / level2 共用一个 worker 线程串行触发,避免 Ollama 模型 swap
    jobs = Jobs(
        level1_services=level1_services,
        level2_services=level2_services,
        poll_interval_seconds=settings.poll_interval_seconds,
    )
    jobs.start()
    logger.info(
        "服务启动成功:summary worker 串行 (level1 batch={} / level2 threshold={},空闲 sleep {}s)",
        settings.batch_size,
        settings.level2_threshold,
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
