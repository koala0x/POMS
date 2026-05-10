from __future__ import annotations

"""
数据接入服务。

对外通过 HTTP /ingest 接口接收三类原始数据,根据 source 派发到对应 ORM 模型,
并在单个事务内批量入库。失败整批回滚,避免出现"半批"数据。

字段约定:
- twitter: content(必填) / author(可选) / posted_at(可选, ISO 8601) / tweet_id(可选, 推文原生 ID)
- binance_square: content(必填) / author(可选) / posted_at(可选, ISO 8601) / post_id(可选, 帖子原生 ID)
- discord: content / channel_name / username(均必填) / posted_at(可选, ISO 8601)

posted_at 不传或解析失败时置为 None,由摘要侧用 created_at 兜底。

twitter / binance_square 都走 PostgreSQL 的 INSERT ... ON CONFLICT (<原生 id>) DO NOTHING,
保证抓取脚本重复跑不会因 UniqueViolation 整批回滚。discord 仍走原本的 session.add_all(),语义不变。
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

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


def _common_raw_fields(item: dict) -> dict:
    """提取 twitter / binance_square 共有字段(content / author / posted_at)的校验逻辑。"""
    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        raise IngestError("content 必填且为非空字符串")
    author = item.get("author")
    if author is not None and not isinstance(author, str):
        raise IngestError("author 必须是字符串或省略")
    return {
        "content": content,
        "author": author,
        "posted_at": _parse_posted_at(item.get("posted_at")),
    }


def _build_twitter_row(item: dict) -> dict:
    """twitter 走 Core insert,所以这里直接返回 dict 而不是 ORM 实例。"""
    fields = _common_raw_fields(item)
    tweet_id = item.get("tweet_id")
    if tweet_id is not None:
        # 强制成 str:Twitter 的 snowflake 在大多数语言里都按字符串传,但 JSON 里偶尔会是 int
        if not isinstance(tweet_id, (str, int)) or (isinstance(tweet_id, str) and not tweet_id.strip()):
            raise IngestError("tweet_id 必须是非空字符串或整数")
        fields["tweet_id"] = str(tweet_id)
    else:
        fields["tweet_id"] = None
    return fields


def _build_binance_row(item: dict) -> dict:
    """binance_square 走 Core insert + ON CONFLICT(post_id),与 twitter 对称。"""
    fields = _common_raw_fields(item)
    post_id = item.get("post_id")
    if post_id is not None:
        # 币安广场帖子 ID 一般是数字字符串,兼容 int 传入
        if not isinstance(post_id, (str, int)) or (isinstance(post_id, str) and not post_id.strip()):
            raise IngestError("post_id 必须是非空字符串或整数")
        fields["post_id"] = str(post_id)
    else:
        fields["post_id"] = None
    return fields


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

    单次请求只处理一种 source,内部用一个事务完成批量插入,失败整批回滚以避免半批写入。
    twitter / binance_square 都走 ON CONFLICT (<原生 id>) DO NOTHING,保证同一条数据重复提交不会失败。
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

        if source == "twitter":
            return self._ingest_twitter(items)
        if source == "discord":
            return self._ingest_orm([_build_discord_message(it) for it in items])
        # binance_square
        return self._ingest_binance(items)

    def _ingest_orm(self, rows: list) -> int:
        with self._db.get_session() as session:
            session.add_all(rows)
            session.commit()
        return len(rows)

    def _ingest_twitter(self, items: list[dict]) -> int:
        """
        twitter 走 Core insert + ON CONFLICT (tweet_id) DO NOTHING。

        - 没传 tweet_id 的行:tweet_id 为 NULL,PG 默认 NULL ≠ NULL,不冲突,正常插入。
        - 传了 tweet_id 的行:同 tweet_id 已存在则跳过,返回值反映实际新增行数。
        """
        rows = [_build_twitter_row(it) for it in items]
        stmt = (
            pg_insert(TwitterPost)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["tweet_id"])
        )
        with self._db.get_session() as session:
            result = session.execute(stmt)
            session.commit()
        # rowcount 为实际写入行数(冲突跳过的不算)
        return int(result.rowcount or 0)

    def _ingest_binance(self, items: list[dict]) -> int:
        """
        binance_square 走 Core insert + ON CONFLICT (post_id) DO NOTHING,
        与 _ingest_twitter 对称:没传 post_id 的行不冲突正常插入,传了的同 id 跳过。
        """
        rows = [_build_binance_row(it) for it in items]
        stmt = (
            pg_insert(BinanceSquarePost)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["post_id"])
        )
        with self._db.get_session() as session:
            result = session.execute(stmt)
            session.commit()
        return int(result.rowcount or 0)
