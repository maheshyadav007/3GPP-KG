"""Scope artifact source/hash uniqueness to a dataset version.

Revision ID: 20260829_0002
Revises: 20260829_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "20260829_0002"
down_revision = "20260829_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    names = _unique_constraint_names()
    if "artifact_versions_source_url_sha256_key" in names:
        op.drop_constraint(
            "artifact_versions_source_url_sha256_key",
            "artifact_versions",
            type_="unique",
        )
    if "uq_artifact_dataset_source_hash" not in names:
        op.create_unique_constraint(
            "uq_artifact_dataset_source_hash",
            "artifact_versions",
            ["dataset_version_id", "source_url", "sha256"],
        )


def downgrade() -> None:
    names = _unique_constraint_names()
    if "uq_artifact_dataset_source_hash" in names:
        op.drop_constraint(
            "uq_artifact_dataset_source_hash",
            "artifact_versions",
            type_="unique",
        )
    if "artifact_versions_source_url_sha256_key" not in names:
        op.create_unique_constraint(
            "artifact_versions_source_url_sha256_key",
            "artifact_versions",
            ["source_url", "sha256"],
        )


def _unique_constraint_names() -> set[str]:
    constraints = sa.inspect(op.get_bind()).get_unique_constraints("artifact_versions")
    return {str(item["name"]) for item in constraints if item.get("name")}
