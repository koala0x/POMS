from __future__ import annotations

"""
HTTP 接入服务入口。

启动后单进程做两件事:
1. Flask /ingest /health 接口,接收外部抓取脚本(币安广场、Discord 等)POST 进来的原始数据
2. 后台线程定时调 twitterapi.io 拉某个 Twitter List 的推文,直接经 IngestService 写库
   - 配置见 config/settings.py 的 twitter_list_* 字段
   - api_key 是占位值时 fetcher 自动跳过

运行:
    python api_main.py             # 开发/调试,Flask 内置服务器,后台 loop 一并启动
    gunicorn -w 1 'api_main:app'   # 生产,**只能 -w 1**,多 worker 会让 fetcher loop 起多份

DB 配置复用 main.py 同一份 settings,确保两端写入同一个库。
"""

import sys
from pathlib import Path

from loguru import logger

from api.server import create_app
from config.settings import Settings, get_settings
from db.connection import Database
from scheduler.twitter_fetcher_loop import TwitterFetcherLoop
from services.ingest_service import IngestService
from services.twitter_fetcher_service import TwitterListFetcherService


def _init_logging(log_path: str, retention_days: int) -> None:
    logger.remove()
    logger.add(sys.stdout, level="INFO", enqueue=True, backtrace=False, diagnose=False)
    logger.add(
        log_path,
        level="INFO",
        rotation="00:00",
        retention=f"{retention_days} days",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )


def _build_fetcher_loop(
    settings: Settings, db: Database, ingest_service: IngestService
) -> TwitterFetcherLoop:
    """构造 Twitter 抓取后台 loop(只构造,start 留给 main() 控制)。"""
    fetcher = TwitterListFetcherService(
        db=db,
        ingest_service=ingest_service,
        api_key=settings.twitterapi_io_key,
        list_id=settings.twitter_list_id,
        max_pages=settings.twitter_list_max_pages,
    )
    return TwitterFetcherLoop(
        fetcher=fetcher,
        interval_seconds=settings.twitter_list_fetch_interval_seconds,
    )


def _build_app():
    """
    构造 Flask app + 依赖。供 gunicorn 'api_main:app' 直接 import 用。

    **注意**:故意 NOT 启动后台 fetcher loop —— 否则 gunicorn 多 worker 时
    每个 worker 都起一份 loop,会重复抓 + 浪费 API 费用。loop 在 main() 里启动,
    走 `python api_main.py` 这条路径才会跑;gunicorn 部署的话需要单独跑一份
    本模块的 main() 进程,或者 gunicorn 用 -w 1。
    """
    settings = get_settings()
    Path(settings.log_path).parent.mkdir(parents=True, exist_ok=True)
    _init_logging(settings.log_path, settings.log_retention_days)

    db = Database(settings)
    # 接入服务也兜底建表一次,防止 main.py 还没启动时新部署 DB 缺表
    db.create_all()
    logger.info("ingest 服务数据库初始化完成")

    ingest_service = IngestService(db=db)
    app = create_app(ingest_service)
    return app, settings, db, ingest_service


# 模块级别变量,gunicorn 通过 'api_main:app' 引用
app, _settings, _db, _ingest_service = _build_app()


def main() -> None:
    # 启动后台 Twitter 抓取 loop。占位 key 时 fetcher 会跳过,不会真的去调 API。
    fetcher_loop = _build_fetcher_loop(_settings, _db, _ingest_service)
    fetcher_loop.start()
    logger.info(
        "Twitter 抓取后台 loop 已启动:间隔 {}s,List={} max_pages={}",
        _settings.twitter_list_fetch_interval_seconds,
        _settings.twitter_list_id,
        _settings.twitter_list_max_pages,
    )

    try:
        # 开发模式直接用 Flask 内置 server;生产请走 gunicorn -w 1
        app.run(
            host=_settings.api_host,
            port=_settings.api_port,
            debug=False,
            use_reloader=False,
        )
    finally:
        # Ctrl+C / 异常退出时让 loop 体面收尾
        fetcher_loop.shutdown(wait=True)


if __name__ == "__main__":
    main()
