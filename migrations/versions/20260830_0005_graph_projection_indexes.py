"""Add indexes for complete meeting graph projections."""

from alembic import op

revision = "20260830_0005"
down_revision = "20260830_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_tdocs_dataset_meeting_id",
        "tdocs",
        ["dataset_version_id", "meeting_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_edges_dataset_source",
        "knowledge_edges",
        ["dataset_version_id", "source_type", "source_id"],
        unique=False,
    )
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
