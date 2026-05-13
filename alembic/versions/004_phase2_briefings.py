"""Phase 2.7: 新增 entity_briefings 表（LLM 定向简报）

Revision ID: 004
Revises: 002
Create Date: 2026-05-14

本迁移对应 spec `phase2-llm-briefing` Phase 2.7 的数据库新增：
- entity_briefings：每 15 分钟 LLM 生成的"为什么热"简报快照

**手写、不用 autogenerate**——只对一张新表操作，绝不触碰已有 9 张表。

字段 / 索引 / 约束定义严格对齐 `db/models.py` 里的 EntityBriefing ORM 类
与 design.md §3.1。

注：版本号跳过 003（spec 把 003 留给 phase2-embedding-clustering，
当前已暂缓）。直接用 004 让本迁移与"3 = embedding 聚类"的预留契合，
不阻塞未来 003 复活。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


# revision identifiers, used by Alembic.
revision = "004"
# 跳过 003（phase2-embedding-clustering 占位，已暂缓），直接接在 002 之后
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 entity_briefings 表 + 1 唯一约束 + 1 条索引。"""
    op.create_table(
        "entity_briefings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("entity", sa.String(128), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("narrative", sa.Text, nullable=True),
        sa.Column("catalyst", sa.Text, nullable=True),
        sa.Column("fund_logic", sa.Text, nullable=True),
        sa.Column("sentiment", sa.String(16), nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column(
            "evidence_msg_ids",
            ARRAY(sa.BigInteger),
            nullable=False,
        ),
        sa.Column("raw_response", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "entity",
            "window_end",
            name="uq_entity_briefings_entity_window",
        ),
    )
    # idx_entity_briefings_window_end：支持"最新窗口的所有简报"查询
    # 给 scripts/check_status.py 未来的 §8 节 / Telegram 渲染时的 fetch_for_entity 用
    op.create_index(
        "idx_entity_briefings_window_end",
        "entity_briefings",
        [sa.text("window_end DESC")],
    )


def downgrade() -> None:
    """drop 表。索引和约束随表 drop 一起被清理，无需单独 drop。"""
    op.drop_table("entity_briefings")
