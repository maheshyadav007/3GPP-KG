"""Add immutable newsletter generations and human review state."""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "20260902_0007"
down_revision = "20260901_0006"
branch_labels = None
depends_on = None


def _packet_hash(value: object) -> str:
    serialized = json.dumps(value or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("newsletters")}
    additions = {
        "status": sa.Column("status", sa.String(32), nullable=True),
        "packet_sha256": sa.Column("packet_sha256", sa.String(64), nullable=True),
        "rendered_sha256": sa.Column("rendered_sha256", sa.String(64)),
        "model": sa.Column("model", sa.String(255)),
        "model_revision": sa.Column("model_revision", sa.String(100)),
        "prompt_version": sa.Column("prompt_version", sa.String(40)),
        "generation_error": sa.Column("generation_error", sa.Text()),
        "reviewed_at": sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        "reviewed_by": sa.Column("reviewed_by", sa.String(255)),
        "review_notes": sa.Column("review_notes", sa.Text()),
    }
    with op.batch_alter_table("newsletters") as batch:
        for name, column in additions.items():
            if name not in columns:
                batch.add_column(column)
    rows = bind.execute(sa.text("SELECT id, packet, rendered FROM newsletters")).mappings()
    for row in rows:
        packet_hash = _packet_hash(row["packet"])
        rendered_hash = _packet_hash(row["rendered"]) if row["rendered"] else None
        bind.execute(
            sa.text(
                "UPDATE newsletters SET status=:status, packet_sha256=:packet_hash, "
                "rendered_sha256=:rendered_hash WHERE id=:id"
            ),
            {
                "id": row["id"],
                "status": "pending_approval" if row["rendered"] else "packet_ready",
                "packet_hash": packet_hash,
                "rendered_hash": rendered_hash,
            },
        )
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("UPDATE newsletters SET rendered = NULL WHERE rendered::text = 'null'")
        )
    columns = {column["name"] for column in sa.inspect(bind).get_columns("newsletters")}
    if "status" in columns and "packet_sha256" in columns:
        with op.batch_alter_table("newsletters") as batch:
            batch.alter_column("status", existing_type=sa.String(32), nullable=False)
            batch.alter_column("packet_sha256", existing_type=sa.String(64), nullable=False)

    inspector = sa.inspect(bind)
    unique_constraints = inspector.get_unique_constraints("newsletters")
    old_constraint = next(
        (
            item
            for item in unique_constraints
            if set(item.get("column_names") or [])
            == {"dataset_version_id", "meeting_id", "edition"}
        ),
        None,
    )
    new_constraint = next(
        (
            item
            for item in unique_constraints
            if set(item.get("column_names") or [])
            == {"dataset_version_id", "meeting_id", "edition", "packet_sha256"}
        ),
        None,
    )
    with op.batch_alter_table("newsletters") as batch:
        if old_constraint and old_constraint.get("name"):
            batch.drop_constraint(old_constraint["name"], type_="unique")
        if not new_constraint:
            batch.create_unique_constraint(
                "uq_newsletter_immutable_edition",
                ["dataset_version_id", "meeting_id", "edition", "packet_sha256"],
            )
    index_names = {item["name"] for item in sa.inspect(bind).get_indexes("newsletters")}
    if "ix_newsletters_status" not in index_names:
        op.create_index("ix_newsletters_status", "newsletters", ["status"])
    if "ix_newsletters_packet_sha256" not in index_names:
        op.create_index("ix_newsletters_packet_sha256", "newsletters", ["packet_sha256"])
    if "ix_newsletters_latest" not in index_names:
        op.create_index(
            "ix_newsletters_latest",
            "newsletters",
            ["dataset_version_id", "meeting_id", "edition", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    with op.batch_alter_table("newsletters") as batch:
        for item in inspector.get_unique_constraints("newsletters"):
            if item.get("name") == "uq_newsletter_immutable_edition":
                batch.drop_constraint(item["name"], type_="unique")
        batch.create_unique_constraint(
            "uq_newsletters_dataset_meeting_edition",
            ["dataset_version_id", "meeting_id", "edition"],
        )
        for name in (
            "review_notes",
            "reviewed_by",
            "reviewed_at",
            "prompt_version",
            "generation_error",
            "model_revision",
            "model",
            "rendered_sha256",
            "packet_sha256",
            "status",
        ):
            batch.drop_column(name)
