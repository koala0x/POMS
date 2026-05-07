from __future__ import annotations

"""
HTTP 接入服务的 Flask 应用工厂。

只暴露一个路由:
    POST /ingest
    Content-Type: application/json
    Body: { "source": "twitter|binance_square|discord", "items": [ ... ] }

设计要点:
- 应用工厂 create_app(ingest_service) 注入服务实例,便于测试时替换 mock。
- 不在路由里做任何业务/写库逻辑,统统委派给 IngestService。
- 错误码:400 = 请求体不合法;500 = 服务端异常(已打日志,响应只回固定字符串)。
"""

from typing import Any

from flask import Flask, Response, jsonify, request
from loguru import logger

from services.ingest_service import IngestError, IngestService


def create_app(ingest_service: IngestService) -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health() -> tuple[Response, int]:
        # 简单的存活检查,不查 DB,避免 DB 慢拖垮 LB 健康探测
        return jsonify(ok=True), 200

    @app.post("/ingest")
    def ingest() -> tuple[Response, int]:
        payload: Any = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="请求体必须是 JSON 对象"), 400

        source = payload.get("source")
        items = payload.get("items")
        if not isinstance(source, str):
            return jsonify(error="source 缺失或不是字符串"), 400

        try:
            inserted = ingest_service.ingest(source, items)  # type: ignore[arg-type]
        except IngestError as e:
            # 客户端字段错误,不打 ERROR 日志,避免噪音淹没真问题
            logger.info("/ingest 拒绝请求: source={} err={}", source, e)
            return jsonify(error=str(e)), 400
        except Exception as e:  # noqa: BLE001
            # 数据库或其它意外异常,完整堆栈进文件日志便于排查
            logger.exception("/ingest 入库失败 source={} err={}", source, e)
            return jsonify(error="internal error"), 500

        logger.info("/ingest source={} inserted={}", source, inserted)
        return jsonify(ok=True, source=source, inserted=inserted), 200

    return app
