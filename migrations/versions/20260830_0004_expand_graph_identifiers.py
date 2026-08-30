"""Expand graph edge identifiers to match knowledge node identifiers.

Revision ID: 20260830_0004
Revises: 20260829_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0004"
down_revision: str | None = "20260829_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "knowledge_edges",
        "source_id",
        existing_type=sa.String(length=100),
        type_=sa.String(length=160),
        existing_nullable=False,
    )
    op.alter_column(
        "knowledge_edges",
        "target_id",
        existing_type=sa.String(length=100),
        type_=sa.String(length=160),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "knowledge_edges",
        "target_id",
        existing_type=sa.String(length=160),
        type_=sa.String(length=100),
        existing_nullable=False,
    )
    op.alter_column(
        "knowledge_edges",
        "source_id",
        existing_type=sa.String(length=160),
        type_=sa.String(length=100),
        existing_nullable=False,
    )
