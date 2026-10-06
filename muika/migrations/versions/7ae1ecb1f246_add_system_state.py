"""add system_state

Revision ID: 7ae1ecb1f246
Revises: 01163f475b84
Create Date: 2026-10-06 13:56:58.352505

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7ae1ecb1f246"
down_revision: str | Sequence[str] | None = "01163f475b84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("system_state")
