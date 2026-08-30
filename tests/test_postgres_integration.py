from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from threegpp_kg.config import DatabaseConfig
from threegpp_kg.constants import DatasetState, EvidenceAuthority
from threegpp_kg.publisher import activate_dataset
from threegpp_kg.repository import SqlRepository
from threegpp_kg.storage.database import (
    DatasetVersionRow,
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
    embedding_a = [1.0, *([0.0] * 1023)]
    embedding_b = [0.0, 1.0, *([0.0] * 1022)]
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE retrieval_chunks, evidence, dataset_versions CASCADE")
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
                    authority=EvidenceAuthority.TDOC_BODY,
                    excerpt="Unrelated scheduling text.",
                ),
            ]
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
                    embedding=embedding_a,
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
                    embedding=embedding_b,
                ),
            ]
        )
        await session.commit()

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
    )
    assert [item.chunk_id for item in semantic] == ["chunk-pg-a", "chunk-pg-b"]

    async with sessions() as session:
        extension = await session.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        indexes = set(
            await session.scalars(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE indexname IN ('ix_chunks_embedding_hnsw', 'ix_tdocs_fulltext')"
                )
            )
        )
    assert extension is not None
    assert indexes == {"ix_chunks_embedding_hnsw", "ix_tdocs_fulltext"}

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
    preview_repository = SqlRepository(
        sessions, preview_dataset_version="postgres-integration-v2"
    )
    assert await preview_repository.active_dataset_version() == "postgres-integration-v2"
    missing_preview = SqlRepository(sessions, preview_dataset_version="missing-version")
    with pytest.raises(RuntimeError, match="does not exist"):
        await missing_preview.active_dataset_version()
    async with sessions() as session:
        await activate_dataset(session, "postgres-integration-v2")
        await session.commit()
    assert await repository.active_dataset_version() == "postgres-integration-v2"
    await engine.dispose()
