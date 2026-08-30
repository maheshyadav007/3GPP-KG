"""Scope artifact source/hash uniqueness to a dataset version.

Revision ID: 20260829_0002
Revises: 20260829_0001
"""

from alembic import op

revision = "20260829_0002"
down_revision = "20260829_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "artifact_versions_source_url_sha256_key",
        "artifact_versions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_artifact_dataset_source_hash",
        "artifact_versions",
        ["dataset_version_id", "source_url", "sha256"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_artifact_dataset_source_hash",
        "artifact_versions",
        type_="unique",
    )
    op.create_unique_constraint(
        "artifact_versions_source_url_sha256_key",
        "artifact_versions",
        ["source_url", "sha256"],
    )
