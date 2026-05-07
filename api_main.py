from __future__ import annotations

"""
HTTP 接入服务入口。

启动后单进程常驻,与 main.py(摘要 worker)解耦运行,生产环境建议:
    python api_main.py             # 开发/调试,Flask 内置服务器
    gunicorn -w 4 -b 0.0.0.0:8080 'api_main:app'   # 生产,多 worker

配置项:
- 监听地址/端口在 Settings 里,默认 0.0.0.0:8080
- DB 配置复用 main.py 同一份 settings,确保两端写入同一个库
"""

import sys
from pathlib import Path

from loguru import logger

from api.server import create_app
from config.settings import get_settings
from db.connection import Database
from services.ingest_service import IngestService


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


def _build_app():
    """构造 Flask app 与依赖。供 gunicorn 直接 import 用。"""
    settings = get_settings()
    Path(settings.log_path).parent.mkdir(parents=True, exist_ok=True)
    _init_logging(settings.log_path, settings.log_retention_days)

    db = Database(settings)
    # 接入服务也兜底建表一次,防止 main.py 还没启动时新部署 DB 缺表
    db.create_all()
    logger.info("ingest 服务数据库初始化完成")

    ingest_service = IngestService(db=db)
    return create_app(ingest_service), settings


# 模块级别变量,gunicorn 通过 'api_main:app' 引用
app, _settings = _build_app()


def main() -> None:
    # 开发模式直接用 Flask 内置 server;生产请走 gunicorn/uwsgi
    app.run(
        host=_settings.api_host,
        port=_settings.api_port,
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
