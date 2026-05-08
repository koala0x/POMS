from __future__ import annotations

"""
ORM 模型定义。

四张表的设计与原 SQL 迁移保持等价:
- twitter_posts / binance_square_posts:原始帖子,带 is_summarized 标记位
- summary_level1:一次摘要(覆盖 50 条原始数据,raw_ids 用 BIGINT[] 保存)
- summary_level2:二次摘要(覆盖一小时内的 N 条 level1,level1_ids 用 BIGINT[] 保存)

使用 SQLAlchemy 2.0 的 Mapped[] / mapped_column 风格;时间字段一律使用
DateTime(timezone=True),对应 PostgreSQL 的 TIMESTAMPTZ。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的统一基类,通过它的 metadata.create_all() 兜底建表。"""


class _RawPostMixin:
    """
    twitter_posts / binance_square_posts 共用列。

    - id:BIGSERIAL 主键
    - content:帖子正文(NOT NULL)
    - author / posted_at:可选,部分数据源可能没有作者或发帖时间
    - created_at:入库时间,默认 NOW()
    - is_summarized:是否已被一次摘要处理,默认 FALSE
    """

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    is_summarized: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )


class TwitterPost(_RawPostMixin, Base):
    """Twitter 原始帖子表。"""

    __tablename__ = "twitter_posts"

    # Twitter 推文的原生 ID(字符串,Twitter 用 snowflake)。可空以兼容老抓取路径,
    # 但只要传入就走 UNIQUE 约束 + ON CONFLICT 去重(scripts/fetch_twitter_list.py 用)。
    # 多个 NULL 在 PostgreSQL 默认行为下不冲突,所以历史数据不需要回填。
    tweet_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index(
            "idx_twitter_posts_summarized_created_at",
            "is_summarized",
            "created_at",
        ),
        UniqueConstraint("tweet_id", name="uq_twitter_posts_tweet_id"),
    )


class BinanceSquarePost(_RawPostMixin, Base):
    """币安广场原始帖子表。"""

    __tablename__ = "binance_square_posts"
    __table_args__ = (
        Index(
            "idx_binance_square_posts_summarized_created_at",
            "is_summarized",
            "created_at",
        ),
    )


class DiscordMessage(Base):
    """
    Discord 聊天记录表。

    字段语义与 _RawPostMixin 一致(id / content / posted_at / created_at / is_summarized),
    但 Discord 的"作者"由 channel_name + username 共同构成,所以单独建模而不复用 mixin:
    - channel_name:频道名(例如 #alpha-calls)
    - username:发言用户名

    为了让 Level1Service 现有 prompt 渲染逻辑(读 author / posted_at)无差别复用,
    暴露一个只读 author 派生属性:`#<channel_name> @<username>`。
    """

    __tablename__ = "discord_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    is_summarized: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    __table_args__ = (
        Index(
            "idx_discord_messages_summarized_created_at",
            "is_summarized",
            "created_at",
        ),
        Index("idx_discord_messages_channel_created_at", "channel_name", "created_at"),
    )

    @property
    def author(self) -> str:
        # Level1Service 的 prompt 拼接默认读 .author,这里把频道+用户名拼成可读串
        return f"#{self.channel_name} @{self.username}"


class SummaryLevel1(Base):
    """
    一次摘要表。

    - source:数据来源('twitter' / 'binance_square'),与原始表对应
    - raw_ids:本批次涉及的原始数据 id 列表(对应原始表主键,逻辑引用,不建外键)
    - is_summarized_l2:是否已被二次摘要消费
    """

    __tablename__ = "summary_level1"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    raw_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    raw_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    is_summarized_l2: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    __table_args__ = (
        Index("idx_summary_level1_source_created_at", "source", "created_at"),
        Index("idx_summary_level1_l2_created_at", "is_summarized_l2", "created_at"),
    )


class SummaryLevel2(Base):
    """
    二次摘要表。

    - level1_ids:本次涉及的 summary_level1.id 列表(逻辑引用)
    - period_start / period_end:覆盖的时间窗口 [start, end)
    """

    __tablename__ = "summary_level2"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    level1_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    level1_count: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_summary_level2_source_created_at", "source", "created_at"),
        Index(
            "idx_summary_level2_source_period",
            "source",
            "period_start",
            "period_end",
        ),
    )


__all__ = [
    "Base",
    "TwitterPost",
    "BinanceSquarePost",
    "DiscordMessage",
    "SummaryLevel1",
    "SummaryLevel2",
]
