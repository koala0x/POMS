"""Phase 1: 新增 normalized_messages / entity_mentions / hotness_snapshots

Revision ID: 001
Revises:
Create Date: 2026-05-11

本迁移对应 spec `crypto-narrative-radar` Phase 1 的数据库新增：
- normalized_messages：L0 归一化 + SimHash 去重后的统一消息
- entity_mentions：L1 实体抽取的提及记录
- hotness_snapshots：L2 每 15 分钟刷新的 Top-K 热度排行榜

**手写、不用 autogenerate**（requirements.md Risk E 兜底）——
只对三张新表操作，绝不触碰现有 5 张表（twitter_posts / binance_square_posts /
discord_messages / summary_level1 / summary_level2）。

字段 / 索引 / 约束定义严格对齐 `db/models.py` 里的 ORM 类与 requirements.md Req 5。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建三张新表 + 10 条索引 + 3 个唯一约束。"""

    # =========================================================================
    # 1. normalized_messages（L0 产出）
    # =========================================================================
    op.create_table(
        "normalized_messages",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("raw_source", sa.String(32), nullable=False),
        sa.Column("raw_id", sa.BigInteger, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column(
            "author_weight",
            sa.Float,
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "engagement",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("simhash", sa.BigInteger, nullable=True),
        sa.Column(
            "sentiment_score",
            sa.Float,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "is_duplicate",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("dup_of", sa.BigInteger, nullable=True),
        sa.Column("l1_processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "raw_source", "raw_id", name="uq_normalized_messages_source_raw"
        ),
    )
    op.create_index(
        "idx_normalized_messages_ts",
        "normalized_messages",
        ["ts"],
    )
    op.create_index(
        "idx_normalized_messages_source_ts",
        "normalized_messages",
        ["raw_source", "ts"],
    )
    op.create_index(
        "idx_normalized_messages_simhash",
        "normalized_messages",
        ["simhash"],
    )
    op.create_index(
        "idx_normalized_messages_is_duplicate_l1_processed_at",
        "normalized_messages",
        ["is_duplicate", "l1_processed_at"],
    )

    # =========================================================================
    # 2. entity_mentions（L1 产出）
    # =========================================================================
    op.create_table(
        "entity_mentions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("msg_id", sa.BigInteger, nullable=False),  # 逻辑引用，不建 FK
        sa.Column("entity", sa.String(128), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=True),
        sa.Column("raw_source", sa.String(32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "engagement",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "author_weight",
            sa.Float,
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column(
            "confidence",
            sa.Float,
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column(
            "is_kol_mention",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.UniqueConstraint(
            "msg_id", "entity", name="uq_entity_mentions_msg_entity"
        ),
    )
    op.create_index(
        "idx_entity_mentions_entity_ts",
        "entity_mentions",
        ["entity", "ts"],
    )
    op.create_index(
        "idx_entity_mentions_ts",
        "entity_mentions",
        ["ts"],
    )
    op.create_index(
        "idx_entity_mentions_source_ts",
        "entity_mentions",
        ["raw_source", "ts"],
    )

    # =========================================================================
    # 3. hotness_snapshots（L2 产出）
    # =========================================================================
    op.create_table(
        "hotness_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_type", sa.String(16), nullable=False),
        sa.Column("entity", sa.String(128), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=True),
        sa.Column("count_short", sa.Integer, nullable=True),
        sa.Column("count_baseline", sa.Float, nullable=True),
        sa.Column("growth_rate", sa.Float, nullable=True),
        sa.Column("cross_source", sa.Integer, nullable=True),
        sa.Column("engagement_sum", sa.Integer, nullable=True),
        sa.Column(
            "is_new_entity",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("final_score", sa.Float, nullable=True),
        sa.Column("rank", sa.Integer, nullable=True),
        sa.UniqueConstraint(
            "window_end",
            "window_type",
            "entity",
            name="uq_hotness_snapshots_window_entity",
        ),
    )
    op.create_index(
        "idx_hotness_snapshots_window_rank",
        "hotness_snapshots",
        ["window_end", "window_type", "rank"],
    )
    op.create_index(
        "idx_hotness_snapshots_entity_window",
        "hotness_snapshots",
        ["entity", "window_end"],
    )


def downgrade() -> None:
    """
    按倒序删除三张表。

    索引和约束会随表 drop 一起被清理，无需单独 drop。
    """
    op.drop_table("hotness_snapshots")
    op.drop_table("entity_mentions")
    op.drop_table("normalized_messages")
