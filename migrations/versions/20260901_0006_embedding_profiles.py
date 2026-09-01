"""Add versioned embedding profiles and dimension-neutral vectors."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "20260901_0006"
down_revision = "20260830_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    vector_type = Vector() if bind.dialect.name == "postgresql" else sa.JSON()
    if "embedding_profiles" not in existing_tables:
        op.create_table(
            "embedding_profiles",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("model", sa.String(length=255), nullable=False),
            sa.Column("revision", sa.String(length=100), nullable=False),
            sa.Column("dimensions", sa.Integer(), nullable=False),
            sa.Column("pooling", sa.String(length=20), nullable=False),
            sa.Column("normalize", sa.Boolean(), nullable=False),
            sa.Column("query_prompt", sa.Text(), nullable=False),
            sa.Column("document_prompt", sa.Text(), nullable=False),
            sa.Column("onnx_sha256", sa.String(length=64), nullable=False),
            sa.Column("runtime_version", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_embedding_profiles_provider", "embedding_profiles", ["provider"])
    if "dataset_embedding_profiles" not in existing_tables:
        op.create_table(
            "dataset_embedding_profiles",
            sa.Column("row_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "dataset_version_id",
                sa.String(length=64),
                sa.ForeignKey("dataset_versions.id"),
                nullable=False,
            ),
            sa.Column(
                "profile_id",
                sa.String(length=64),
                sa.ForeignKey("embedding_profiles.id"),
                nullable=False,
            ),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("total_chunks", sa.Integer(), nullable=False),
            sa.Column("embedded_chunks", sa.Integer(), nullable=False),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("activated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("dataset_version_id", "profile_id"),
        )
        op.create_index(
            "ix_dataset_embedding_profiles_dataset_version_id",
            "dataset_embedding_profiles",
            ["dataset_version_id"],
        )
        op.create_index(
            "ix_dataset_embedding_profiles_profile_id",
            "dataset_embedding_profiles",
            ["profile_id"],
        )
        op.create_index(
            "ix_dataset_embedding_profiles_state", "dataset_embedding_profiles", ["state"]
        )
        op.create_index(
            "ix_dataset_embedding_profiles_is_active",
            "dataset_embedding_profiles",
            ["is_active"],
        )
        if bind.dialect.name == "postgresql":
            op.execute(
                "CREATE UNIQUE INDEX ix_dataset_embedding_active "
                "ON dataset_embedding_profiles (dataset_version_id, is_active) "
                "WHERE is_active"
            )
        else:
            op.create_index(
                "ix_dataset_embedding_active",
                "dataset_embedding_profiles",
                ["dataset_version_id", "is_active"],
            )
    if "chunk_embeddings" not in existing_tables:
        op.create_table(
            "chunk_embeddings",
            sa.Column("row_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "dataset_version_id",
                sa.String(length=64),
                sa.ForeignKey("dataset_versions.id"),
                nullable=False,
            ),
            sa.Column("chunk_id", sa.String(length=100), nullable=False),
            sa.Column(
                "profile_id",
                sa.String(length=64),
                sa.ForeignKey("embedding_profiles.id"),
                nullable=False,
            ),
            sa.Column("dimensions", sa.Integer(), nullable=False),
            sa.Column("embedding", vector_type, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("dataset_version_id", "chunk_id", "profile_id"),
        )
        op.create_index(
            "ix_chunk_embeddings_dataset_version_id",
            "chunk_embeddings",
            ["dataset_version_id"],
        )
        op.create_index("ix_chunk_embeddings_chunk_id", "chunk_embeddings", ["chunk_id"])
        op.create_index("ix_chunk_embeddings_profile_id", "chunk_embeddings", ["profile_id"])
        op.create_index(
            "ix_chunk_embeddings_dataset_profile",
            "chunk_embeddings",
            ["dataset_version_id", "profile_id", "chunk_id"],
        )
    chunk_columns = {column["name"] for column in sa.inspect(bind).get_columns("retrieval_chunks")}
    if bind.dialect.name == "postgresql" and "embedding" in chunk_columns:
        legacy_count = int(
            bind.scalar(
                sa.text("SELECT count(*) FROM retrieval_chunks WHERE embedding IS NOT NULL")
            )
            or 0
        )
        if legacy_count:
            op.execute(
                sa.text(
                    """
                    INSERT INTO embedding_profiles (
                        id, provider, model, revision, dimensions, pooling, normalize,
                        query_prompt, document_prompt, onnx_sha256, runtime_version, created_at
                    ) VALUES (
                        'legacy-1024', 'openai_compatible', 'legacy-unknown',
                        'endpoint-managed', 1024, 'auto', false, '', '',
                        :checksum, 'unknown', now()
                    ) ON CONFLICT (id) DO NOTHING
                    """
                ).bindparams(checksum="0" * 64)
            )
            op.execute(
                """
                INSERT INTO chunk_embeddings (
                    dataset_version_id, chunk_id, profile_id, dimensions, embedding, created_at
                )
                SELECT dataset_version_id, id, 'legacy-1024', 1024, embedding, now()
                FROM retrieval_chunks
                WHERE embedding IS NOT NULL
                ON CONFLICT (dataset_version_id, chunk_id, profile_id) DO NOTHING
                """
            )
            op.execute(
                """
                INSERT INTO dataset_embedding_profiles (
                    dataset_version_id, profile_id, state, is_active, total_chunks,
                    embedded_chunks, created_at
                )
                SELECT dataset_version_id, 'legacy-1024',
                       CASE WHEN count(*) FILTER (WHERE embedding IS NOT NULL) = count(*)
                            THEN 'validated' ELSE 'failed' END,
                       false, count(*), count(*) FILTER (WHERE embedding IS NOT NULL), now()
                FROM retrieval_chunks
                GROUP BY dataset_version_id
                HAVING count(*) FILTER (WHERE embedding IS NOT NULL) > 0
                ON CONFLICT (dataset_version_id, profile_id) DO NOTHING
                """
            )
        op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    if "embedding" in chunk_columns:
        with op.batch_alter_table("retrieval_chunks") as batch:
            batch.drop_column("embedding")


def downgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("retrieval_chunks") as batch:
        batch.add_column(
            sa.Column(
                "embedding",
                Vector(1024) if bind.dialect.name == "postgresql" else sa.JSON(),
                nullable=True,
            )
        )
    op.drop_table("chunk_embeddings")
    op.drop_table("dataset_embedding_profiles")
    op.drop_table("embedding_profiles")
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_chunks_embedding_hnsw "
            "ON retrieval_chunks USING hnsw (embedding vector_cosine_ops)"
        )
