from __future__ import annotations

"""
程序入口。

启动流程：
- 读取配置（.env）
- 初始化日志（控制台 + 文件按天滚动）
- 初始化 DB / LLM / 各层 service
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


def _ensure_raw_tables(db: Database) -> None:
    """
    启动时确保两张原始表存在（兜底建表）。

    约束：
    - 只做幂等创建（IF NOT EXISTS / ADD COLUMN IF NOT EXISTS）
    - 不尝试变更既有列类型/约束，避免误伤已有数据
    - 需要 DB 用户具备建表/建索引权限，否则会启动失败
    """

    ddl_statements = [
        """
        CREATE TABLE IF NOT EXISTS twitter_posts (
            id BIGSERIAL PRIMARY KEY,
            content TEXT NOT NULL,
            author VARCHAR,
            posted_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            is_summarized BOOLEAN NOT NULL DEFAULT FALSE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS binance_square_posts (
            id BIGSERIAL PRIMARY KEY,
            content TEXT NOT NULL,
            author VARCHAR,
            posted_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            is_summarized BOOLEAN NOT NULL DEFAULT FALSE
        );
        """,
        "ALTER TABLE twitter_posts ADD COLUMN IF NOT EXISTS is_summarized BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE binance_square_posts ADD COLUMN IF NOT EXISTS is_summarized BOOLEAN NOT NULL DEFAULT FALSE;",
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'twitter_posts'
              AND column_name = 'created_at'
              AND table_schema = ANY (current_schemas(true))
          ) THEN
            EXECUTE 'CREATE INDEX IF NOT EXISTS idx_twitter_posts_is_summarized_created_at ON twitter_posts (is_summarized, created_at);';
          ELSE
            EXECUTE 'CREATE INDEX IF NOT EXISTS idx_twitter_posts_is_summarized_id ON twitter_posts (is_summarized, id);';
          END IF;
        END $$;
        """,
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'binance_square_posts'
              AND column_name = 'created_at'
              AND table_schema = ANY (current_schemas(true))
          ) THEN
            EXECUTE 'CREATE INDEX IF NOT EXISTS idx_binance_square_posts_is_summarized_created_at ON binance_square_posts (is_summarized, created_at);';
          ELSE
            EXECUTE 'CREATE INDEX IF NOT EXISTS idx_binance_square_posts_is_summarized_id ON binance_square_posts (is_summarized, id);';
          END IF;
        END $$;
        """,
    ]

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for stmt in ddl_statements:
                cur.execute(stmt)
        conn.commit()


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

    # 初始化 DB，并确保原始表存在（避免新库启动时报“表不存在”）
    db = Database(settings)
    _ensure_raw_tables(db)
    ollama = OllamaClient(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_seconds=settings.ollama_timeout_seconds,
        retry_times=settings.ollama_retry_times,
        retry_delay_seconds=settings.ollama_retry_delay_seconds,
    )

    # 仓储层：原始表（twitter/binance）与摘要表（level1/level2）
    twitter_repo = TwitterRepo()
    binance_repo = BinanceRepo()
    level1_repo = Level1Repo()
    level2_repo = Level2Repo()

    # Prompt 模板目录
    base_dir = Path(__file__).resolve().parent
    prompts_dir = base_dir / "prompts"

    # 业务层：分别为两个 source 构建 service（互不合并）
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

    # 调度层：注册并启动定时任务
    jobs = Jobs(
        level1_services=level1_services,
        level2_services=level2_services,
        poll_interval_seconds=settings.poll_interval_seconds,
        timezone=settings.timezone,
    )
    scheduler = jobs.start()
    logger.info("服务启动成功：poll_interval={}s timezone={}", settings.poll_interval_seconds, settings.timezone)

    try:
        # BackgroundScheduler 在后台线程运行，这里只需要保持主线程常驻
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("收到退出信号，正在停止调度器...")
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
