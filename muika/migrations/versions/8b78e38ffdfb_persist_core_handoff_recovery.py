"""persist core handoff recovery

Revision ID: 8b78e38ffdfb
Revises: f25cd59dbab4
Create Date: 2026-10-02 23:02:56.527074

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8b78e38ffdfb"
down_revision: str | Sequence[str] | None = "f25cd59dbab4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存跨重启的交接目标、偏好期限和恢复确认。"""
    with op.batch_alter_table("runtime_authority", schema=None) as batch_op:
        batch_op.add_column(sa.Column("handoff", sa.Text(), nullable=True))


def downgrade() -> None:
    """移除交接记录，保留部署身份和控制权任期。"""
    with op.batch_alter_table("runtime_authority", schema=None) as batch_op:
        batch_op.drop_column("handoff")
