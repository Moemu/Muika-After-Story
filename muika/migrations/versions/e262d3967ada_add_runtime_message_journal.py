"""add runtime message journal

Revision ID: e262d3967ada
Revises: 5e446de27cb4
Create Date: 2026-10-02 13:29:45.020185

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e262d3967ada"
down_revision: str | Sequence[str] | None = "5e446de27cb4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加运行时控制权、收件箱和发件箱。"""
    op.create_table(
        "runtime_authority",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cluster_id", sa.String(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "runtime_inbox",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("commit_digest", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("client_id", "message_id"),
    )
    with op.batch_alter_table("runtime_inbox", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runtime_inbox_status"), ["status"], unique=False)

    op.create_table(
        "runtime_outbox",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("acknowledged", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("message_id"),
    )
    with op.batch_alter_table("runtime_outbox", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runtime_outbox_client_id"), ["client_id"], unique=False)


def downgrade() -> None:
    """移除运行时消息表，保留已有记忆与任务。"""
    with op.batch_alter_table("runtime_outbox", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runtime_outbox_client_id"))

    op.drop_table("runtime_outbox")
    with op.batch_alter_table("runtime_inbox", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runtime_inbox_status"))

    op.drop_table("runtime_inbox")
    op.drop_table("runtime_authority")
