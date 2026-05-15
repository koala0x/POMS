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
    TwitterPost,
)


def test_tables_registered() -> None:
    """八张业务表都应注册在 metadata 上。

    历史变更（2026-05）：老链路淘汰，metadata 不再注册 summary_level1 /
    summary_level2 ORM；DB 里两张表数据保留作历史归档。
    """
    expected = {
        # 三张原始表（抓取服务写入；新链路只读）
        "twitter_posts",
        "binance_square_posts",
        "discord_messages",
        # Phase 1：crypto-narrative-radar L0/L1/L2
        "normalized_messages",
        "entity_mentions",
        "hotness_snapshots",
        # Phase 2.5 新增：L3 实体共现网络
        "entity_cooccurrence",
        # Phase 2.7 新增：L5 LLM 定向简报
        "entity_briefings",
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


def test_indexes_defined() -> None:
    """关键查询路径上的索引应在 metadata 中注册。"""
    expected_indexes = {
        "twitter_posts": {"idx_twitter_posts_summarized_created_at"},
        "binance_square_posts": {"idx_binance_square_posts_summarized_created_at"},
    }
    for table_name, names in expected_indexes.items():
        table = Base.metadata.tables[table_name]
        actual = {idx.name for idx in table.indexes}
        assert names.issubset(actual), f"{table_name} 缺少索引:期望 {names},实际 {actual}"


def test_postgres_ddl_rendering() -> None:
    """
    在 PostgreSQL 方言下编译 DDL,应渲染出预期的关键字:
    - BIGSERIAL(自增主键)、TIMESTAMP WITH TIME ZONE
    """
    dialect = postgresql.dialect()

    twitter_ddl = str(CreateTable(TwitterPost.__table__).compile(dialect=dialect))
    assert "BIGSERIAL" in twitter_ddl  # autoincrement 主键
    assert "TIMESTAMP WITH TIME ZONE" in twitter_ddl  # created_at / posted_at


# ============================================================================
# Phase 1 新表（crypto-narrative-radar）的结构性测试
# ============================================================================


def test_normalized_messages_columns() -> None:
    """NormalizedMessage 字段集合与 requirements.md Req 5.2 对齐。"""
    from db.models import NormalizedMessage

    columns = {c.name: c for c in inspect(NormalizedMessage).columns}
    expected = {
        "id", "raw_source", "raw_id", "text", "author", "author_weight",
        "ts", "engagement", "simhash", "sentiment_score",
        "is_duplicate", "dup_of", "l1_processed_at", "created_at",
    }
    assert set(columns) == expected
    # 关键 nullable 约束
    assert columns["raw_source"].nullable is False
    assert columns["raw_id"].nullable is False
    assert columns["text"].nullable is False
    assert columns["ts"].nullable is False
    assert columns["l1_processed_at"].nullable is True  # 未处理时 NULL
    assert columns["is_duplicate"].nullable is False


def test_normalized_messages_constraints_and_indexes() -> None:
    """NormalizedMessage 的唯一约束与 5 条索引（Req 5.3）。"""
    from db.models import NormalizedMessage

    table = NormalizedMessage.__table__
    unique_cols = {
        tuple(c.name for c in uc.columns)
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("raw_source", "raw_id") in unique_cols

    idx_names = {idx.name for idx in table.indexes}
    expected_indexes = {
        "idx_normalized_messages_ts",
        "idx_normalized_messages_source_ts",
        "idx_normalized_messages_simhash",
        "idx_normalized_messages_is_duplicate_l1_processed_at",
    }
    assert expected_indexes.issubset(idx_names), (
        f"缺少索引：期望 {expected_indexes}，实际 {idx_names}"
    )


def test_entity_mentions_columns_and_constraints() -> None:
    """EntityMention 字段 + UNIQUE(msg_id, entity) 幂等键（Req 5.4, 5.6）。"""
    from db.models import EntityMention

    columns = {c.name: c for c in inspect(EntityMention).columns}
    expected = {
        "id", "msg_id", "entity", "entity_type", "raw_source", "ts",
        "engagement", "author_weight", "confidence", "is_kol_mention",
    }
    assert set(columns) == expected

    table = EntityMention.__table__
    unique_cols = {
        tuple(c.name for c in uc.columns)
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    # Req 5.6 的幂等写入靠这条
    assert ("msg_id", "entity") in unique_cols

    idx_names = {idx.name for idx in table.indexes}
    expected_indexes = {
        "idx_entity_mentions_entity_ts",
        "idx_entity_mentions_ts",
        "idx_entity_mentions_source_ts",
    }
    assert expected_indexes.issubset(idx_names)


def test_hotness_snapshots_columns_and_constraints() -> None:
    """HotnessSnapshot 字段 + UNIQUE(window_end, window_type, entity)（Req 5.7）。"""
    from db.models import HotnessSnapshot

    columns = {c.name: c for c in inspect(HotnessSnapshot).columns}
    expected = {
        "id", "window_end", "window_type", "entity", "entity_type",
        "count_short", "count_baseline", "growth_rate", "cross_source",
        "engagement_sum", "is_new_entity", "final_score", "rank",
    }
    assert set(columns) == expected

    table = HotnessSnapshot.__table__
    unique_cols = {
        tuple(c.name for c in uc.columns)
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("window_end", "window_type", "entity") in unique_cols

    idx_names = {idx.name for idx in table.indexes}
    expected_indexes = {
        "idx_hotness_snapshots_window_rank",
        "idx_hotness_snapshots_entity_window",
    }
    assert expected_indexes.issubset(idx_names)


def test_phase1_tables_have_no_foreign_keys() -> None:
    """
    Req 5.10：新三张表不对现有 5 张表建立任何外键依赖，
    entity_mentions.msg_id → normalized_messages.id 也只做逻辑引用。
    """
    from db.models import EntityMention, HotnessSnapshot, NormalizedMessage

    for model in (NormalizedMessage, EntityMention, HotnessSnapshot):
        fks = list(model.__table__.foreign_keys)
        assert fks == [], f"{model.__name__} 不应有外键，实际：{fks}"


def test_phase1_tables_postgres_ddl_style() -> None:
    """Req 5.11：新三张表在 PostgreSQL 方言下渲染出 BIGSERIAL + TIMESTAMPTZ。"""
    from db.models import EntityMention, HotnessSnapshot, NormalizedMessage

    dialect = postgresql.dialect()
    for model in (NormalizedMessage, EntityMention, HotnessSnapshot):
        ddl = str(CreateTable(model.__table__).compile(dialect=dialect))
        assert "BIGSERIAL" in ddl, f"{model.__name__} 主键应为 BIGSERIAL"
        assert "TIMESTAMP WITH TIME ZONE" in ddl, (
            f"{model.__name__} ts 字段应为 TIMESTAMPTZ"
        )
