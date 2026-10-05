"""persist local activity synchronization

Revision ID: 01163f475b84
Revises: 5e446de27cb4
Create Date: 2026-10-05 19:41:42.163882

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "01163f475b84"
down_revision: str | Sequence[str] | None = "5e446de27cb4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建本地活动日志、来源映射和同步进度表。"""
    op.create_table(
        "sync_event",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("gateway_sequence", sa.Integer(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("id"),
    )
    op.create_table(
        "sync_reference",
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("local_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("source"),
    )
    op.create_table(
        "sync_state",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    """移除同步表，保留本地记忆和行动记录。"""
    op.drop_table("sync_state")
    op.drop_table("sync_reference")
    op.drop_table("sync_event")
