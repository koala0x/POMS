"""Phase 2.5: 新增 entity_cooccurrence 表（实体共现网络）

Revision ID: 002
Revises: 001
Create Date: 2026-05-14

本迁移对应 spec `phase2-cooccurrence-network` Phase 2.5 的数据库新增：
- entity_cooccurrence：L3 每 15 分钟刷新的实体两两共现统计 + PMI

**手写、不用 autogenerate**——只对一张新表操作，绝不触碰已有 8 张表。

字段 / 索引 / 约束定义严格对齐 `db/models.py` 里的 EntityCooccurrence ORM 类
与 design.md §3.1。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 entity_cooccurrence 表 + 1 唯一约束 + 3 条索引。"""
    op.create_table(
        "entity_cooccurrence",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("entity_a", sa.String(128), nullable=False),
        sa.Column("entity_b", sa.String(128), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_type", sa.String(16), nullable=False),
        sa.Column("cooccur_count", sa.Integer, nullable=False),
        sa.Column("pmi", sa.Float, nullable=True),
        sa.Column(
            "is_new_pair",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "entity_a",
            "entity_b",
            "window_end",
            "window_type",
            name="uq_cooccur_pair_window",
        ),
    )
    # idx_cooccur_window_pmi：支持"最新窗口的 Top-K PMI 对"查询
    # （scripts/check_status.py §5 / fetch_top_k_pairs 用到）
    op.create_index(
        "idx_cooccur_window_pmi",
        "entity_cooccurrence",
        ["window_end", "window_type", sa.text("pmi DESC")],
    )
    # idx_cooccur_entity_a / entity_b：支持 fetch_neighbors 双侧查询
    # （某 entity 既可能在 entity_a 也可能在 entity_b 侧）
    op.create_index(
        "idx_cooccur_entity_a",
        "entity_cooccurrence",
        ["entity_a", sa.text("window_end DESC")],
    )
    op.create_index(
        "idx_cooccur_entity_b",
        "entity_cooccurrence",
        ["entity_b", sa.text("window_end DESC")],
    )


def downgrade() -> None:
    """drop 表。索引和约束随表 drop 一起被清理，无需单独 drop。"""
    op.drop_table("entity_cooccurrence")
