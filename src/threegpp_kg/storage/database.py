from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..config import DatabaseConfig


class Base(DeclarativeBase):
    pass


class DatasetVersionRow(Base):
    __tablename__ = "dataset_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MeetingRow(Base):
    __tablename__ = "meetings"

    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(80), index=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    working_group_id: Mapped[str] = mapped_column(String(20), index=True)
    number: Mapped[int] = mapped_column(Integer, index=True)
    variant: Mapped[str] = mapped_column(String(40), default="")
    name: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(Text)
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    readiness: Mapped[str] = mapped_column(String(32), index=True)

    __table_args__ = (UniqueConstraint("dataset_version_id", "id"),)


class ArtifactVersionRow(Base):
    __tablename__ = "artifact_versions"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    meeting_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    content_type: Mapped[str] = mapped_column(String(255))
    etag: Mapped[str | None] = mapped_column(String(255))
    last_modified: Mapped[str | None] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(Text)
    parse_status: Mapped[str] = mapped_column(String(20), default="parsed", index=True)
    parse_error: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id",
            "source_url",
            "sha256",
            name="uq_artifact_dataset_source_hash",
        ),
    )


class TDocRow(Base):
    __tablename__ = "tdocs"

    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(40), index=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    meeting_id: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="")
    document_type: Mapped[str] = mapped_column(String(80), default="", index=True)
    purpose: Mapped[str] = mapped_column(String(80), default="")
    agenda_item: Mapped[str] = mapped_column(String(80), default="", index=True)
    agenda_description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), index=True)
    status_raw: Mapped[str] = mapped_column(String(80), default="")
    abstract: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    discussion: Mapped[str] = mapped_column(Text, default="")
    conclusion_text: Mapped[str] = mapped_column(Text, default="")
    revised_from: Mapped[str | None] = mapped_column(String(40), index=True)
    revised_to: Mapped[str | None] = mapped_column(String(40), index=True)
    releases: Mapped[list[str]] = mapped_column(JSON, default=list)
    specifications: Mapped[list[str]] = mapped_column(JSON, default=list)
    work_items: Mapped[list[str]] = mapped_column(JSON, default=list)
    cr_number: Mapped[str | None] = mapped_column(String(40), index=True)
    cr_revision: Mapped[str | None] = mapped_column(String(20))
    cr_category: Mapped[str | None] = mapped_column(String(20))
    source_url: Mapped[str | None] = mapped_column(Text)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    __table_args__ = (
        UniqueConstraint("dataset_version_id", "id"),
        Index("ix_tdocs_title_source", "dataset_version_id", "status"),
        Index("ix_tdocs_dataset_meeting_id", "dataset_version_id", "meeting_id", "id"),
    )


class EvidenceRow(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    artifact_sha256: Mapped[str] = mapped_column(String(64), index=True)
    authority: Mapped[str] = mapped_column(String(40), index=True)
    meeting_id: Mapped[str | None] = mapped_column(String(80), index=True)
    tdoc_id: Mapped[str | None] = mapped_column(String(40), index=True)
    section_path: Mapped[list[str]] = mapped_column(JSON, default=list)
    block_id: Mapped[str | None] = mapped_column(String(100), index=True)
    excerpt: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[str] = mapped_column(String(80), default="parser")
    extractor_version: Mapped[str] = mapped_column(String(80), default="0.1.0")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    meeting_time: Mapped[date | None] = mapped_column(Date)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class DocumentBlockRow(Base):
    __tablename__ = "document_blocks"

    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(100), index=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    document_id: Mapped[str] = mapped_column(String(100), index=True)
    block_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    text: Mapped[str] = mapped_column(Text)
    section_path: Mapped[list[str]] = mapped_column(JSON, default=list)
    table_row: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("dataset_version_id", "id"),
        UniqueConstraint("dataset_version_id", "document_id", "block_index"),
    )


class RetrievalChunkRow(Base):
    __tablename__ = "retrieval_chunks"

    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(100), index=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    document_id: Mapped[str] = mapped_column(String(100), index=True)
    block_ids: Mapped[list[str]] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text)
    section_path: Mapped[list[str]] = mapped_column(JSON, default=list)
    token_count: Mapped[int] = mapped_column(Integer)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    __table_args__ = (UniqueConstraint("dataset_version_id", "id"),)


class EmbeddingProfileRow(Base):
    __tablename__ = "embedding_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(255))
    revision: Mapped[str] = mapped_column(String(100))
    dimensions: Mapped[int] = mapped_column(Integer)
    pooling: Mapped[str] = mapped_column(String(20), default="auto")
    normalize: Mapped[bool] = mapped_column(Boolean, default=True)
    query_prompt: Mapped[str] = mapped_column(Text, default="")
    document_prompt: Mapped[str] = mapped_column(Text, default="")
    onnx_sha256: Mapped[str] = mapped_column(String(64))
    runtime_version: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class DatasetEmbeddingProfileRow(Base):
    __tablename__ = "dataset_embedding_profiles"

    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("embedding_profiles.id"), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0)
    embedded_chunks: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("dataset_version_id", "profile_id"),
        Index(
            "ix_dataset_embedding_active",
            "dataset_version_id",
            "is_active",
            unique=True,
            postgresql_where=(is_active.is_(True)),
            sqlite_where=(is_active.is_(True)),
        ),
    )


class ChunkEmbeddingRow(Base):
    __tablename__ = "chunk_embeddings"

    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    chunk_id: Mapped[str] = mapped_column(String(100), index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("embedding_profiles.id"), index=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector().with_variant(JSON(), "sqlite"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint("dataset_version_id", "chunk_id", "profile_id"),
        Index(
            "ix_chunk_embeddings_dataset_profile",
            "dataset_version_id",
            "profile_id",
            "chunk_id",
        ),
    )


class TopicRow(Base):
    __tablename__ = "topics"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    canonical_name: Mapped[str] = mapped_column(String(255), index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(40), default="agenda")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)


class KnowledgeNodeRow(Base):
    __tablename__ = "knowledge_nodes"

    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(40), index=True)
    id: Mapped[str] = mapped_column(String(160), index=True)
    label: Mapped[str] = mapped_column(String(512))
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (UniqueConstraint("dataset_version_id", "entity_type", "id"),)


class NewsletterRow(Base):
    __tablename__ = "newsletters"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    meeting_id: Mapped[str] = mapped_column(String(80), index=True)
    edition: Mapped[str] = mapped_column(String(20), index=True)
    packet: Mapped[dict[str, Any]] = mapped_column(JSON)
    rendered: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), index=True, default="packet_ready")
    packet_sha256: Mapped[str] = mapped_column(String(64), index=True)
    rendered_sha256: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(255))
    model_revision: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    generation_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id",
            "meeting_id",
            "edition",
            "packet_sha256",
            name="uq_newsletter_immutable_edition",
        ),
        Index(
            "ix_newsletters_latest",
            "dataset_version_id",
            "meeting_id",
            "edition",
            "created_at",
        ),
    )


class KnowledgeEdgeRow(Base):
    __tablename__ = "knowledge_edges"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[str] = mapped_column(String(160), index=True)
    predicate: Mapped[str] = mapped_column(String(40), index=True)
    target_type: Mapped[str] = mapped_column(String(40), index=True)
    target_id: Mapped[str] = mapped_column(String(160), index=True)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    valid_at: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id", "source_id", "predicate", "target_id", name="uq_edge_fact"
        ),
        Index(
            "ix_edges_dataset_source",
            "dataset_version_id",
            "source_type",
            "source_id",
        ),
        Index(
            "ix_edges_dataset_target",
            "dataset_version_id",
            "predicate",
            "target_id",
            "source_id",
        ),
    )


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(40), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(30), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    leased_by: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)


def create_engine_and_session(
    config: DatabaseConfig,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    kwargs: dict[str, Any] = {"echo": config.echo}
    if not config.url.startswith("sqlite"):
        kwargs.update(
            pool_size=config.pool_size, max_overflow=config.max_overflow, pool_pre_ping=True
        )
    engine = create_async_engine(config.url, **kwargs)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
