"""add agent task checkpoints

Revision ID: 3b6a7e5f332e
Revises: 906676bdf27e
Create Date: 2026-09-06 00:35:36.019784

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b6a7e5f332e"
down_revision: str | Sequence[str] | None = "906676bdf27e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建任务和动作检查点表。"""
    op.create_table(
        "agent_task",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_agent_task_status", "agent_task", ["status"])
    op.create_index("ix_agent_task_created_at", "agent_task", ["created_at"])
    op.create_table(
        "agent_call",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_agent_call_task_id", "agent_call", ["task_id"])
    op.create_index("ix_agent_call_status", "agent_call", ["status"])


def downgrade() -> None:
    """移除任务索引，保留持久目录中的输出文件。"""
    op.drop_table("agent_call")
    op.drop_table("agent_task")
