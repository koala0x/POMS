from __future__ import annotations

"""
程序入口。

启动流程:
- 读取配置(.env)
- 初始化日志(控制台 + 文件按天滚动)
- 初始化 DB(SQLAlchemy Engine + Session)并建表
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

    # 初始化 DB,并通过 ORM metadata 兜底建表(幂等)。
    # 所有表/索引定义都在 db/models.py 中,变更只需修改模型,无需手写 DDL。
    db = Database(settings)
    db.create_all()
    logger.info("数据库初始化完成(create_all 已执行)")

    ollama = OllamaClient(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_seconds=settings.ollama_timeout_seconds,
        retry_times=settings.ollama_retry_times,
        retry_delay_seconds=settings.ollama_retry_delay_seconds,
    )

    # 仓储层:原始表(twitter/binance)与摘要表(level1/level2)
    twitter_repo = TwitterRepo()
    binance_repo = BinanceRepo()
    level1_repo = Level1Repo()
    level2_repo = Level2Repo()

    # Prompt 模板目录
    base_dir = Path(__file__).resolve().parent
    prompts_dir = base_dir / "prompts"

    # 业务层:分别为两个 source 构建 service(互不合并)
    level1_services = [
        Level1Service(
            db=db,
            source="twitter",
            batch_size=settings.batch_size,
            raw_repo=twitter_repo,
            level1_repo=level1_repo,
            ollama=ollama,
            prompt_path=prompts_dir / "level1_twitter.txt",
            timezone=settings.timezone,
        ),
        Level1Service(
            db=db,
            source="binance_square",
            batch_size=settings.batch_size,
            raw_repo=binance_repo,
            level1_repo=level1_repo,
            ollama=ollama,
            prompt_path=prompts_dir / "level1_binance.txt",
            timezone=settings.timezone,
        ),
    ]

    level2_services = [
        Level2Service(
            db=db,
            source="twitter",
            level1_repo=level1_repo,
            level2_repo=level2_repo,
            ollama=ollama,
            prompt_path=prompts_dir / "level2_twitter.txt",
            timezone=settings.timezone,
        ),
        Level2Service(
            db=db,
            source="binance_square",
            level1_repo=level1_repo,
            level2_repo=level2_repo,
            ollama=ollama,
            prompt_path=prompts_dir / "level2_binance.txt",
            timezone=settings.timezone,
        ),
    ]

    # 调度层:注册并启动定时任务
    jobs = Jobs(
        level1_services=level1_services,
        level2_services=level2_services,
        poll_interval_seconds=settings.poll_interval_seconds,
        timezone=settings.timezone,
    )
    scheduler = jobs.start()
    logger.info("服务启动成功:poll_interval={}s timezone={}", settings.poll_interval_seconds, settings.timezone)

    try:
        # BackgroundScheduler 在后台线程运行,这里只需要保持主线程常驻
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("收到退出信号,正在停止调度器...")
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
