from __future__ import annotations

"""
数据接入服务。

对外通过 HTTP /ingest 接口接收三类原始数据,根据 source 派发到对应 ORM 模型,
并在单个事务内批量入库。失败整批回滚,避免出现"半批"数据。

字段约定:
- twitter / binance_square: content(必填) / author(可选) / posted_at(可选, ISO 8601)
- discord: content / channel_name / username(均必填) / posted_at(可选, ISO 8601)

posted_at 不传或解析失败时置为 None,由摘要侧用 created_at 兜底。
"""

from datetime import datetime, timezone
from typing import Any

from db.connection import Database
from db.models import BinanceSquarePost, DiscordMessage, TwitterPost


class IngestError(ValueError):
    """请求体不合法时抛出,由路由层捕获并返回 400。"""


_RAW_MODEL_BY_SOURCE = {
    "twitter": TwitterPost,
    "binance_square": BinanceSquarePost,
}


def _parse_posted_at(raw: Any) -> datetime | None:
    # 不传/空字符串 → None,业务侧用 created_at 兜底
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise IngestError(f"posted_at 必须是 ISO 8601 字符串,收到 {type(raw).__name__}")
    # Python 3.11+ 的 fromisoformat 支持 'Z' 后缀;兼容老格式则手动替换
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as e:
        raise IngestError(f"posted_at 格式不合法: {raw}") from e
    # 没带 tz 的当作 UTC,避免写库时 TIMESTAMPTZ 异常
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_raw_post(source: str, item: dict) -> TwitterPost | BinanceSquarePost:
    model = _RAW_MODEL_BY_SOURCE[source]
    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        raise IngestError("content 必填且为非空字符串")
    author = item.get("author")
    if author is not None and not isinstance(author, str):
        raise IngestError("author 必须是字符串或省略")
    return model(
        content=content,
        author=author,
        posted_at=_parse_posted_at(item.get("posted_at")),
    )


def _build_discord_message(item: dict) -> DiscordMessage:
    content = item.get("content")
    channel_name = item.get("channel_name")
    username = item.get("username")
    if not isinstance(content, str) or not content.strip():
        raise IngestError("content 必填且为非空字符串")
    if not isinstance(channel_name, str) or not channel_name.strip():
        raise IngestError("discord 数据 channel_name 必填且为非空字符串")
    if not isinstance(username, str) or not username.strip():
        raise IngestError("discord 数据 username 必填且为非空字符串")
    return DiscordMessage(
        content=content,
        channel_name=channel_name,
        username=username,
        posted_at=_parse_posted_at(item.get("posted_at")),
    )


class IngestService:
    """
    接收外部提交的原始数据并批量入库。

    单次请求只处理一种 source,内部用一个事务 + add_all 完成批量插入,
    失败整批回滚以避免半批写入。
    """

    SUPPORTED_SOURCES = ("twitter", "binance_square", "discord")

    def __init__(self, db: Database) -> None:
        self._db = db

    def ingest(self, source: str, items: list[dict]) -> int:
        if source not in self.SUPPORTED_SOURCES:
            raise IngestError(
                f"source 必须是 {self.SUPPORTED_SOURCES} 之一,收到 {source!r}"
            )
        if not isinstance(items, list):
            raise IngestError("items 必须是数组")
        if not items:
            return 0

        if source == "discord":
            rows = [_build_discord_message(it) for it in items]
        else:
            rows = [_build_raw_post(source, it) for it in items]

        with self._db.get_session() as session:
            session.add_all(rows)
            session.commit()
        return len(rows)
