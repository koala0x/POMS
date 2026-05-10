from __future__ import annotations

"""
ORM 模型结构性测试,不依赖真实 DB。

验证:
- 表名、列、索引都按预期定义
- TwitterPost / BinanceSquarePost 共享同一组列(mixin 工作正常)
- 类型在 PostgreSQL 方言下渲染为期望的 DDL(BIGINT、TIMESTAMPTZ、BIGINT[])
"""

from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from db.models import (
    Base,
    BinanceSquarePost,
    SummaryLevel1,
    SummaryLevel2,
    TwitterPost,
)


def test_tables_registered() -> None:
    """五张业务表都应注册在 metadata 上。"""
    expected = {
        "twitter_posts",
        "binance_square_posts",
        "discord_messages",
        "summary_level1",
        "summary_level2",
    }
    assert expected == set(Base.metadata.tables.keys())


def test_raw_post_mixin_shared_columns() -> None:
    """
    Twitter / Binance 两表都应包含 mixin 的核心列;
    Twitter / Binance 各自多一列原生 ID(tweet_id / post_id),用于抓取侧去重。
    """
    mixin_cols = {
        "id",
        "content",
        "author",
        "posted_at",
        "created_at",
        "is_summarized",
    }
    twitter_cols = {c.name for c in inspect(TwitterPost).columns}
    binance_cols = {c.name for c in inspect(BinanceSquarePost).columns}
    assert binance_cols == mixin_cols | {"post_id"}
    assert twitter_cols == mixin_cols | {"tweet_id"}


def test_twitter_tweet_id_unique() -> None:
    """tweet_id 必须有 UNIQUE 约束,fetcher 才能依赖 ON CONFLICT 做去重。"""
    table = TwitterPost.__table__
    unique_cols = {
        tuple(c.name for c in uc.columns)
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("tweet_id",) in unique_cols
    assert table.c.tweet_id.nullable is True  # 兼容老路径,可空


def test_binance_post_id_unique() -> None:
    """post_id 必须有 UNIQUE 约束,抓取脚本可依赖 ON CONFLICT 做去重。"""
    table = BinanceSquarePost.__table__
    unique_cols = {
        tuple(c.name for c in uc.columns)
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("post_id",) in unique_cols
    assert table.c.post_id.nullable is True  # 兼容老路径,可空


def test_discord_post_id_unique() -> None:
    """discord_messages.post_id 必须有 UNIQUE 约束,抓取侧依赖 ON CONFLICT 做去重。"""
    from db.models import DiscordMessage

    table = DiscordMessage.__table__
    unique_cols = {
        tuple(c.name for c in uc.columns)
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("post_id",) in unique_cols
    assert table.c.post_id.nullable is True  # 兼容老路径,可空


def test_raw_post_constraints() -> None:
    """关键 nullable / 默认值约束正确。"""
    columns = {c.name: c for c in inspect(TwitterPost).columns}
    assert columns["content"].nullable is False
    assert columns["author"].nullable is True
    assert columns["posted_at"].nullable is True
    assert columns["created_at"].nullable is False
    assert columns["is_summarized"].nullable is False
    # is_summarized 默认 false
    assert columns["is_summarized"].default is not None
    assert columns["is_summarized"].default.arg is False


def test_summary_level1_columns() -> None:
    columns = {c.name: c for c in inspect(SummaryLevel1).columns}
    assert set(columns) == {
        "id",
        "source",
        "summary",
        "raw_ids",
        "raw_count",
        "created_at",
        "is_summarized_l2",
    }
    assert columns["raw_ids"].nullable is False
    assert columns["raw_count"].nullable is False
    assert columns["is_summarized_l2"].default.arg is False


def test_summary_level2_columns() -> None:
    columns = {c.name: c for c in inspect(SummaryLevel2).columns}
    assert set(columns) == {
        "id",
        "source",
        "summary",
        "level1_ids",
        "level1_count",
        "period_start",
        "period_end",
        "created_at",
    }
    assert columns["period_start"].nullable is False
    assert columns["period_end"].nullable is False


def test_indexes_defined() -> None:
    """关键查询路径上的索引应在 metadata 中注册。"""
    expected_indexes = {
        "twitter_posts": {"idx_twitter_posts_summarized_created_at"},
        "binance_square_posts": {"idx_binance_square_posts_summarized_created_at"},
        "summary_level1": {
            "idx_summary_level1_source_created_at",
            "idx_summary_level1_l2_created_at",
        },
        "summary_level2": {
            "idx_summary_level2_source_created_at",
            "idx_summary_level2_source_period",
        },
    }
    for table_name, names in expected_indexes.items():
        table = Base.metadata.tables[table_name]
        actual = {idx.name for idx in table.indexes}
        assert names.issubset(actual), f"{table_name} 缺少索引:期望 {names},实际 {actual}"


def test_postgres_ddl_rendering() -> None:
    """
    在 PostgreSQL 方言下编译 DDL,应渲染出预期的关键字:
    - BIGSERIAL(自增主键)、TIMESTAMP WITH TIME ZONE、BIGINT[]
    """
    dialect = postgresql.dialect()

    twitter_ddl = str(CreateTable(TwitterPost.__table__).compile(dialect=dialect))
    assert "BIGSERIAL" in twitter_ddl  # autoincrement 主键
    assert "TIMESTAMP WITH TIME ZONE" in twitter_ddl  # created_at / posted_at

    level1_ddl = str(CreateTable(SummaryLevel1.__table__).compile(dialect=dialect))
    assert "BIGINT[]" in level1_ddl  # raw_ids
    assert "TIMESTAMP WITH TIME ZONE" in level1_ddl

    level2_ddl = str(CreateTable(SummaryLevel2.__table__).compile(dialect=dialect))
    assert "BIGINT[]" in level2_ddl  # level1_ids
    assert "TIMESTAMP WITH TIME ZONE" in level2_ddl
