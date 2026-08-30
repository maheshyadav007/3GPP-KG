"""Initial evidence graph schema."""

from alembic import op

from threegpp_kg.storage.database import Base

revision = "20260829_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw "
            "ON retrieval_chunks USING hnsw (embedding vector_cosine_ops)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_tdocs_fulltext ON tdocs USING gin "
            "(to_tsvector('english', coalesce(title,'') || ' ' || coalesce(abstract,'') || "
            "' ' || coalesce(summary,'') || ' ' || coalesce(discussion,'')))"
        )


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
