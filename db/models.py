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
    Float,
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
    """所有 ORM 模型的统一基类;本服务只做查询/写入,不依赖 metadata.create_all()。"""


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

    # 币安广场帖子的原生 ID(字符串)。可空以兼容老抓取路径,传入时走 UNIQUE + ON CONFLICT 去重。
    # 与 twitter_posts.tweet_id 设计一致:多个 NULL 在 PostgreSQL 默认行为下不冲突,历史数据无需回填。
    post_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index(
            "idx_binance_square_posts_summarized_created_at",
            "is_summarized",
            "created_at",
        ),
        UniqueConstraint("post_id", name="uq_binance_square_posts_post_id"),
    )


class DiscordMessage(Base):
    """
    Discord 聊天记录表。

    字段语义与 _RawPostMixin 一致(id / content / posted_at / created_at / is_summarized),
    但 Discord 的"作者"由 channel_name + username 共同构成,所以单独建模而不复用 mixin:
    - channel_name:频道名(例如 #alpha-calls)
    - username:发言用户名

    post_id 是 Discord 侧的原生消息标识(抓取脚本通常用 `<channel_id>-<message_id>` 的复合
    形式,例如 "1234567890-9876543210"),加 UNIQUE 约束后可走 INSERT ... ON CONFLICT
    (post_id) DO NOTHING 做入库去重,和 twitter_posts.tweet_id / binance_square_posts.post_id
    的设计完全对称。多个 NULL 在 PostgreSQL 默认行为下不冲突,历史数据无需回填。

    为了让 Level1Service 现有 prompt 渲染逻辑(读 author / posted_at)无差别复用,
    暴露一个只读 author 派生属性:`#<channel_name> @<username>`。
    """

    __tablename__ = "discord_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Discord 消息的原生复合 ID,可空以兼容老抓取路径,传入时走 UNIQUE + ON CONFLICT 去重
    post_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
        UniqueConstraint("post_id", name="uq_discord_messages_post_id"),
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


# ============================================================================
# Phase 1 新增：加密叙事雷达（crypto-narrative-radar）
# ----------------------------------------------------------------------------
# 以下三张表对应 spec 的 L0/L1/L2 层产出：
#   - NormalizedMessage：L0 归一化 + SimHash 去重后的统一消息结构
#   - EntityMention：L1 实体抽取的提及记录（每个 NormalizedMessage 可能挂 0~N 条）
#   - HotnessSnapshot：L2 每 15 分钟刷新一次的 Top-K 增长率排行榜
#
# 设计约束（见 requirements.md Req 5.9、Req 5.10）：
#   - **不对现有 5 张表建立任何外键**（老链路保持独立）
#   - `EntityMention.msg_id -> NormalizedMessage.id` 只做逻辑引用，应用层保证一致性
#   - ORM 风格严格对齐 _RawPostMixin：DateTime(timezone=True)、server_default=func.now()、
#     索引命名 idx_<table>_<cols>、唯一约束 uq_<table>_<cols>
# ============================================================================


class NormalizedMessage(Base):
    """
    L0 归一化后的标准消息。

    来源：消费 twitter_posts / binance_square_posts / discord_messages 后产出。
    消费端：
      - EntityExtractor 扫 is_duplicate=FALSE 且 l1_processed_at IS NULL 的消息
      - SlidingCounter 启动回填 simhash 索引

    字段说明：
      - raw_source + raw_id：指向来源表的逻辑引用（不建 FK，应用层幂等）
      - author：三源统一后的作者字符串，Discord 为 "#<channel> @<user>"
      - author_weight：预留给 KOL 权重，Phase 1 全部 = 1.0
      - ts：消息发布/创建时间（UTC 归一）
      - engagement：点赞+转发+回复汇总，Phase 1 三源都暂无，全部 = 0
      - simhash：64 位 SimHash 指纹，用于 24h 内近似去重
      - sentiment_score：[-1, +1]，Phase 1 全部 = 0（Non-Goals §11）
      - is_duplicate / dup_of：SimHash 判重结果
      - l1_processed_at：L1 实体抽取完成时间戳（NULL = 未处理）
                         比布尔字段信息更丰富，Phase 2 可用于诊断 L1 延迟
    """

    __tablename__ = "normalized_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    raw_source: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_weight: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="1.0"
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    engagement: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sentiment_score: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="0"
    )
    is_duplicate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    dup_of: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    l1_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "raw_source", "raw_id", name="uq_normalized_messages_source_raw"
        ),
        Index("idx_normalized_messages_ts", "ts"),
        Index("idx_normalized_messages_source_ts", "raw_source", "ts"),
        Index("idx_normalized_messages_simhash", "simhash"),
        Index(
            "idx_normalized_messages_is_duplicate_l1_processed_at",
            "is_duplicate",
            "l1_processed_at",
        ),
    )


class EntityMention(Base):
    """
    L1 实体提及记录。

    一对多关系：一条 NormalizedMessage 可挂 0~N 条 EntityMention。
    msg_id 是 NormalizedMessage.id 的**逻辑引用**，不建外键（requirements.md Req 5.10）。

    幂等写入由 UNIQUE(msg_id, entity) 保证：
      - Entity_Extractor 对同一消息反复处理时走 ON CONFLICT DO NOTHING
      - 满足 requirements.md Req 4.8

    字段说明：
      - entity：实体标准名（ticker 大写如 "BTC"，chain 保留原形如 "Base"）
      - entity_type：ticker / chain / narrative / project / kol 之一
      - confidence：Phase 1 只允许 1.0（词典命中）或 0.95（正则命中）
      - is_kol_mention：author 是否命中 kols.yaml；Phase 1 只打标不计分
      - engagement / author_weight：快照式冗余存储，避免后续 JOIN NormalizedMessage
    """

    __tablename__ = "entity_mentions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    msg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entity: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_source: Mapped[str] = mapped_column(String(32), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    engagement: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    author_weight: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="1.0"
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="1.0"
    )
    is_kol_mention: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    __table_args__ = (
        UniqueConstraint("msg_id", "entity", name="uq_entity_mentions_msg_entity"),
        Index("idx_entity_mentions_entity_ts", "entity", "ts"),
        Index("idx_entity_mentions_ts", "ts"),
        Index("idx_entity_mentions_source_ts", "raw_source", "ts"),
    )


class HotnessSnapshot(Base):
    """
    L2 热度快照。

    每 15 分钟 Hotness_Service 触发一次，对 Top-K 实体做 UPSERT 写入。
    幂等键：(window_end, window_type, entity)。

    字段说明：
      - window_end：本轮对齐到 :00/:15/:30/:45 的窗口结束时刻
      - window_type：Phase 1 固定 "1h"；Phase 2 会引入 "6h"/"24h" 多窗口
      - count_short：短窗（默认 1h）内提及次数
      - count_baseline：基线期每小时平均提及次数（float，可能是小数）
      - growth_rate：short_count / max(baseline_per_hour, 2.0)
      - cross_source：短窗内出现过的独立 raw_source 个数（1~3）
      - engagement_sum：短窗内 engagement 累加（Phase 1 三源都为 0）
      - is_new_entity：baseline=0 且 short_count >= 5
      - final_score：growth_rate * (1 + 0.3 * (cross_source - 1))
      - rank：1~top_k，按 final_score 降序（多级稳定排序见 Req 7.10）
    """

    __tablename__ = "hotness_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    count_short: Mapped[int | None] = mapped_column(Integer, nullable=True)
    count_baseline: Mapped[float | None] = mapped_column(Float, nullable=True)
    growth_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    cross_source: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engagement_sum: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_new_entity: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    final_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "window_end",
            "window_type",
            "entity",
            name="uq_hotness_snapshots_window_entity",
        ),
        Index(
            "idx_hotness_snapshots_window_rank",
            "window_end",
            "window_type",
            "rank",
        ),
        Index("idx_hotness_snapshots_entity_window", "entity", "window_end"),
    )


__all__ = [
    "Base",
    "TwitterPost",
    "BinanceSquarePost",
    "DiscordMessage",
    "SummaryLevel1",
    "SummaryLevel2",
    "NormalizedMessage",
    "EntityMention",
    "HotnessSnapshot",
]
