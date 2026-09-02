"""Add versioned high-value meeting sources and observations."""

import sqlalchemy as sa
from alembic import op

revision = "20260902_0008"
down_revision = "20260902_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("artifact_versions")}
    additions = {
        "source_role": sa.Column("source_role", sa.String(40), nullable=True),
        "logical_document_id": sa.Column("logical_document_id", sa.String(160)),
        "document_id": sa.Column("document_id", sa.String(160)),
        "document_state": sa.Column("document_state", sa.String(32), nullable=True),
        "published_at": sa.Column("published_at", sa.DateTime(timezone=True)),
    }
    with op.batch_alter_table("artifact_versions") as batch:
        for name, column in additions.items():
            if name not in columns:
                batch.add_column(column)
    bind.execute(
        sa.text(
            "UPDATE artifact_versions SET source_role=kind "
            "WHERE source_role IS NULL"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE artifact_versions SET document_state='published' "
            "WHERE document_state IS NULL"
        )
    )
    with op.batch_alter_table("artifact_versions") as batch:
        batch.alter_column("source_role", existing_type=sa.String(40), nullable=False)
        batch.alter_column("document_state", existing_type=sa.String(32), nullable=False)

    index_names = {item["name"] for item in sa.inspect(bind).get_indexes("artifact_versions")}
    for name, columns_ in (
        ("ix_artifact_versions_source_role", ["source_role"]),
        ("ix_artifact_versions_logical_document_id", ["logical_document_id"]),
        ("ix_artifact_versions_document_id", ["document_id"]),
        ("ix_artifact_versions_document_state", ["document_state"]),
    ):
        if name not in index_names:
            op.create_index(name, "artifact_versions", columns_)

    if "meeting_observations" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "meeting_observations",
            sa.Column("id", sa.String(120), primary_key=True),
            sa.Column(
                "dataset_version_id",
                sa.String(64),
                sa.ForeignKey("dataset_versions.id"),
                nullable=False,
            ),
            sa.Column("meeting_id", sa.String(80), nullable=False),
            sa.Column("artifact_version_id", sa.String(100), nullable=False),
            sa.Column("source_role", sa.String(40), nullable=False),
            sa.Column("authority", sa.String(40), nullable=False),
            sa.Column("observation_type", sa.String(40), nullable=False),
            sa.Column("observation_key", sa.String(64), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("agenda_item", sa.String(80), nullable=False, server_default=""),
            sa.Column("tdoc_ids", sa.JSON(), nullable=False),
            sa.Column("specification_ids", sa.JSON(), nullable=False),
            sa.Column("work_item_ids", sa.JSON(), nullable=False),
            sa.Column("conclusion", sa.String(40)),
            sa.Column("evidence_ids", sa.JSON(), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("effective_at", sa.DateTime(timezone=True)),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
            sa.UniqueConstraint(
                "dataset_version_id",
                "artifact_version_id",
                "observation_type",
                "observation_key",
                "content_hash",
                name="uq_meeting_observation_source_content",
            ),
        )
        for column in (
            "dataset_version_id",
            "meeting_id",
            "artifact_version_id",
            "source_role",
            "authority",
            "observation_type",
            "observation_key",
            "agenda_item",
            "conclusion",
            "content_hash",
        ):
            op.create_index(
                f"ix_meeting_observations_{column}", "meeting_observations", [column]
            )
        op.create_index(
            "ix_meeting_observations_briefing",
            "meeting_observations",
            ["dataset_version_id", "meeting_id", "observation_type"],
        )


def downgrade() -> None:
    op.drop_table("meeting_observations")
    for name in (
        "ix_artifact_versions_document_state",
        "ix_artifact_versions_document_id",
        "ix_artifact_versions_logical_document_id",
        "ix_artifact_versions_source_role",
    ):
        op.drop_index(name, table_name="artifact_versions")
    with op.batch_alter_table("artifact_versions") as batch:
        for name in (
            "published_at",
            "document_state",
            "document_id",
            "logical_document_id",
            "source_role",
        ):
            batch.drop_column(name)
