"""add rss digest cache

Revision ID: aa5d6045b34e
Revises: 27385c0c4fbb
Create Date: 2026-07-31 14:11:17.131963

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa5d6045b34e"
down_revision: str | Sequence[str] | None = "27385c0c4fbb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rss_digest_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("topic_id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("link", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("published", sa.String(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("keep", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("primary_theme", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.String(), nullable=False),
        sa.Column("evaluated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("rss_digest_cache", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_rss_digest_cache_topic_id"), ["topic_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_rss_digest_cache_source_id"), ["source_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("rss_digest_cache", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_rss_digest_cache_source_id"))
        batch_op.drop_index(batch_op.f("ix_rss_digest_cache_topic_id"))

    op.drop_table("rss_digest_cache")
