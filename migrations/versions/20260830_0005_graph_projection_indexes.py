"""Add indexes for complete meeting graph projections."""

import sqlalchemy as sa
from alembic import op

revision = "20260830_0005"
down_revision = "20260830_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tdoc_indexes = {item["name"] for item in inspector.get_indexes("tdocs")}
    if "ix_tdocs_dataset_meeting_id" not in tdoc_indexes:
        op.create_index(
            "ix_tdocs_dataset_meeting_id",
            "tdocs",
            ["dataset_version_id", "meeting_id", "id"],
            unique=False,
        )
    edge_indexes = {item["name"] for item in inspector.get_indexes("knowledge_edges")}
    if "ix_edges_dataset_source" not in edge_indexes:
        op.create_index(
            "ix_edges_dataset_source",
            "knowledge_edges",
            ["dataset_version_id", "source_type", "source_id"],
            unique=False,
        )
    if "ix_edges_dataset_target" not in edge_indexes:
        op.create_index(
            "ix_edges_dataset_target",
            "knowledge_edges",
            ["dataset_version_id", "predicate", "target_id", "source_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_edges_dataset_target", table_name="knowledge_edges")
    op.drop_index("ix_edges_dataset_source", table_name="knowledge_edges")
    op.drop_index("ix_tdocs_dataset_meeting_id", table_name="tdocs")
