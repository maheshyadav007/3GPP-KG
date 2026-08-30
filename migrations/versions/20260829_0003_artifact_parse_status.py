"""Record stable parse and quarantine status for artifact versions.

Revision ID: 20260829_0003
Revises: 20260829_0002
"""

import sqlalchemy as sa
from alembic import op

revision = "20260829_0003"
down_revision = "20260829_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "artifact_versions",
        sa.Column("parse_status", sa.String(length=20), nullable=False, server_default="parsed"),
    )
    op.add_column("artifact_versions", sa.Column("parse_error", sa.Text(), nullable=True))
    op.create_index(
        "ix_artifact_versions_parse_status",
        "artifact_versions",
        ["parse_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_versions_parse_status", table_name="artifact_versions")
    op.drop_column("artifact_versions", "parse_error")
    op.drop_column("artifact_versions", "parse_status")
