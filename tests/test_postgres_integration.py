from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from threegpp_kg.config import DatabaseConfig
from threegpp_kg.constants import DatasetState, EvidenceAuthority
from threegpp_kg.embedding_backfill import (
    _backfill_lock,
    activate_embedding_profile,
    create_profile_hnsw_index,
)
from threegpp_kg.publisher import activate_dataset
from threegpp_kg.repository import SqlRepository
from threegpp_kg.storage.database import (
    ChunkEmbeddingRow,
    DatasetEmbeddingProfileRow,
    DatasetVersionRow,
    EmbeddingProfileRow,
    EvidenceRow,
    RetrievalChunkRow,
    create_engine_and_session,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_pgvector_fulltext_and_dataset_binding() -> None:
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    database_name = make_url(database_url).database or ""
    if not database_name.endswith("_test"):
        pytest.fail("TEST_POSTGRES_URL must target a database ending in _test")

    engine, sessions = create_engine_and_session(DatabaseConfig(url=database_url))
    embedding_a = [1.0, 0.0, 0.0]
    embedding_b = [0.0, 1.0, 0.0]
    profile_id = "emb-" + "2" * 32
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE chunk_embeddings, dataset_embedding_profiles, embedding_profiles, "
                "retrieval_chunks, evidence, dataset_versions CASCADE"
            )
        )
    async with sessions() as session:
        session.add(
            DatasetVersionRow(
                id="postgres-integration-v1",
                state=DatasetState.ACTIVE,
                created_at=datetime.now(UTC),
                activated_at=datetime.now(UTC),
                is_active=True,
                stats={},
            )
        )
        await session.flush()
        session.add_all(
            [
                EvidenceRow(
                    id="ev-pg-a",
                    dataset_version_id="postgres-integration-v1",
                    source_url="https://www.3gpp.org/test-a",
                    artifact_sha256="a" * 64,
                    authority=EvidenceAuthority.APPROVED_REPORT,
                    excerpt="Mobility robustness was agreed.",
                ),
                EvidenceRow(
                    id="ev-pg-b",
                    dataset_version_id="postgres-integration-v1",
                    source_url="https://www.3gpp.org/test-b",
                    artifact_sha256="b" * 64,
                    authority=EvidenceAuthority.APPROVED_REPORT,
                    excerpt="Unrelated scheduling text.",
                ),
            ]
        )
        session.add(
            EmbeddingProfileRow(
                id=profile_id,
                provider="onnx",
                model="integration/model",
                revision="a" * 40,
                dimensions=3,
                pooling="cls",
                normalize=True,
                query_prompt="",
                document_prompt="",
                onnx_sha256="c" * 64,
                runtime_version="test",
                created_at=datetime.now(UTC),
            )
        )
        await session.flush()
        session.add(
            DatasetEmbeddingProfileRow(
                dataset_version_id="postgres-integration-v1",
                profile_id=profile_id,
                state="active",
                is_active=True,
                total_chunks=2,
                embedded_chunks=2,
                created_at=datetime.now(UTC),
                activated_at=datetime.now(UTC),
            )
        )
        session.add_all(
            [
                RetrievalChunkRow(
                    id="chunk-pg-a",
                    dataset_version_id="postgres-integration-v1",
                    document_id="R2-TEST-A",
                    block_ids=["block-a"],
                    text="The meeting agreed mobility robustness improvements.",
                    section_path=["7.2", "Mobility"],
                    token_count=7,
                    evidence_ids=["ev-pg-a"],
                ),
                RetrievalChunkRow(
                    id="chunk-pg-b",
                    dataset_version_id="postgres-integration-v1",
                    document_id="R2-TEST-B",
                    block_ids=["block-b"],
                    text="Scheduling details for an unrelated procedure.",
                    section_path=["8.1", "Scheduling"],
                    token_count=6,
                    evidence_ids=["ev-pg-b"],
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                ChunkEmbeddingRow(
                    dataset_version_id="postgres-integration-v1",
                    chunk_id="chunk-pg-a",
                    profile_id=profile_id,
                    dimensions=3,
                    embedding=embedding_a,
                    created_at=datetime.now(UTC),
                ),
                ChunkEmbeddingRow(
                    dataset_version_id="postgres-integration-v1",
                    chunk_id="chunk-pg-b",
                    profile_id=profile_id,
                    dimensions=3,
                    embedding=embedding_b,
                    created_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()

    profile_index = await create_profile_hnsw_index(engine, profile_id, 3)

    repository = SqlRepository(sessions)
    assert await repository.active_dataset_version() == "postgres-integration-v1"
    lexical = await repository.search_passages(
        "mobility robustness",
        tdoc_ids=[],
        meeting_ids=[],
        top_k=2,
    )
    assert [item.chunk_id for item in lexical] == ["chunk-pg-a"]
    semantic = await repository.search_passages(
        "",
        tdoc_ids=[],
        meeting_ids=[],
        top_k=2,
        query_embedding=embedding_a,
        embedding_profile_id=profile_id,
    )
    assert [item.chunk_id for item in semantic] == ["chunk-pg-a", "chunk-pg-b"]

    profile_b = "emb-" + "3" * 32
    async with sessions() as session:
        session.add(
            EmbeddingProfileRow(
                id=profile_b,
                provider="onnx",
                model="integration/model-b",
                revision="b" * 40,
                dimensions=2,
                pooling="mean",
                normalize=True,
                query_prompt="",
                document_prompt="",
                onnx_sha256="d" * 64,
                runtime_version="test",
                created_at=datetime.now(UTC),
            )
        )
        await session.flush()
        session.add(
            DatasetEmbeddingProfileRow(
                dataset_version_id="postgres-integration-v1",
                profile_id=profile_b,
                state="validated",
                is_active=False,
                total_chunks=2,
                embedded_chunks=2,
                created_at=datetime.now(UTC),
            )
        )
        session.add_all(
            [
                ChunkEmbeddingRow(
                    dataset_version_id="postgres-integration-v1",
                    chunk_id="chunk-pg-a",
                    profile_id=profile_b,
                    dimensions=2,
                    embedding=[0.0, 1.0],
                    created_at=datetime.now(UTC),
                ),
                ChunkEmbeddingRow(
                    dataset_version_id="postgres-integration-v1",
                    chunk_id="chunk-pg-b",
                    profile_id=profile_b,
                    dimensions=2,
                    embedding=[1.0, 0.0],
                    created_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()
    profile_b_index = await create_profile_hnsw_index(engine, profile_b, 2)
    await activate_embedding_profile(sessions, "postgres-integration-v1", profile_b)
    active_profile = await repository.active_embedding_profile()
    assert active_profile is not None
    assert active_profile.id == profile_b
    assert active_profile.dimensions == 2
    semantic_b = await repository.search_passages(
        "",
        tdoc_ids=[],
        meeting_ids=[],
        top_k=2,
        query_embedding=[1.0, 0.0],
        embedding_profile_id=profile_b,
    )
    assert [item.chunk_id for item in semantic_b] == ["chunk-pg-b", "chunk-pg-a"]

    async with sessions() as session:
        extension = await session.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        indexes = set(
            await session.scalars(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE indexname IN (:profile_index, :profile_b_index, 'ix_tdocs_fulltext')"
                ).bindparams(
                    profile_index=profile_index,
                    profile_b_index=profile_b_index,
                )
            )
        )
    assert extension is not None
    assert indexes == {profile_index, profile_b_index, "ix_tdocs_fulltext"}

    async with sessions() as session:
        session.add(
            DatasetVersionRow(
                id="postgres-integration-v2",
                state=DatasetState.VALIDATED,
                created_at=datetime.now(UTC),
                is_active=False,
                stats={},
            )
        )
        await session.commit()
    preview_repository = SqlRepository(sessions, preview_dataset_version="postgres-integration-v2")
    assert await preview_repository.active_dataset_version() == "postgres-integration-v2"
    missing_preview = SqlRepository(sessions, preview_dataset_version="missing-version")
    with pytest.raises(RuntimeError, match="does not exist"):
        await missing_preview.active_dataset_version()
    async with sessions() as session:
        await activate_dataset(session, "postgres-integration-v2")
        await session.commit()
    assert await repository.active_dataset_version() == "postgres-integration-v2"
    await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_advisory_lock_does_not_block_concurrent_index() -> None:
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    database_name = make_url(database_url).database or ""
    if not database_name.endswith("_test"):
        pytest.fail("TEST_POSTGRES_URL must target a database ending in _test")

    engine, _ = create_engine_and_session(DatabaseConfig(url=database_url))
    profile_id = "emb-" + "4" * 32
    async with _backfill_lock(engine, "lock-regression", profile_id):
        index_name = await asyncio.wait_for(
            create_profile_hnsw_index(engine, profile_id, 2), timeout=5
        )
    assert index_name is not None
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}"))
    await engine.dispose()
