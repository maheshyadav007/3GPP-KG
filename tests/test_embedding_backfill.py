from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from threegpp_kg.config import DatabaseConfig, ModelEndpointConfig
from threegpp_kg.constants import DatasetState, EmbeddingProfileState
from threegpp_kg.embedding_backfill import EmbeddingBackfillError, backfill_embeddings
from threegpp_kg.storage.database import (
    Base,
    ChunkEmbeddingRow,
    DatasetEmbeddingProfileRow,
    DatasetVersionRow,
    EmbeddingProfileRow,
    RetrievalChunkRow,
    create_engine_and_session,
)


class FakeEmbeddingClient:
    profile_id = "emb-" + "1" * 32
    model_name = "test/model"
    revision = "a" * 40
    dimensions = 3

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def available(self) -> bool:
        return True

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self.embed_documents(texts)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("injected provider failure")
        return [[1.0, 0.0, 0.0] for _ in texts]


def embedding_config(tmp_path) -> ModelEndpointConfig:
    return ModelEndpointConfig(
        provider="onnx",
        model="test/model",
        revision="a" * 40,
        dimensions=3,
        cache_dir=tmp_path,
        batch_size=2,
    )


async def seeded_database(tmp_path):
    database = DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'embeddings.db'}")
    engine, sessions = create_engine_and_session(database)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(
            DatasetVersionRow(
                id="dataset-v1",
                state=DatasetState.ACTIVE,
                created_at=datetime.now(UTC),
                activated_at=datetime.now(UTC),
                is_active=True,
                stats={},
            )
        )
        session.add_all(
            [
                RetrievalChunkRow(
                    id=f"chunk-{index}",
                    dataset_version_id="dataset-v1",
                    document_id="R2-TEST",
                    block_ids=[f"block-{index}"],
                    text=f"Technical passage {index}",
                    section_path=["1"],
                    token_count=index + 1,
                    evidence_ids=[],
                )
                for index in range(3)
            ]
        )
        await session.commit()
    return engine, sessions


@pytest.mark.asyncio
async def test_backfill_is_complete_resumable_and_atomically_activated(tmp_path) -> None:
    engine, sessions = await seeded_database(tmp_path)
    client = FakeEmbeddingClient()
    manifest: dict[str, Any] = {"onnx_sha256": "b" * 64, "runtime_version": "test"}
    first = await backfill_embeddings(
        engine,
        sessions,
        embedding_config(tmp_path),
        "dataset-v1",
        client,
        manifest,
        activate=True,
    )
    second = await backfill_embeddings(
        engine,
        sessions,
        embedding_config(tmp_path),
        "dataset-v1",
        client,
        manifest,
        activate=True,
    )

    assert first["embedded_now"] == 3
    assert first["coverage"] == 1.0
    assert second["embedded_now"] == 0
    async with sessions() as session:
        assert await session.scalar(select(func.count(ChunkEmbeddingRow.row_id))) == 3
        assignment = await session.scalar(select(DatasetEmbeddingProfileRow))
        assert assignment is not None
        assert assignment.state == EmbeddingProfileState.ACTIVE
        assert assignment.is_active is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_backfill_records_failure_without_activation(tmp_path) -> None:
    engine, sessions = await seeded_database(tmp_path)
    with pytest.raises(RuntimeError, match="injected provider failure"):
        await backfill_embeddings(
            engine,
            sessions,
            embedding_config(tmp_path),
            "dataset-v1",
            FakeEmbeddingClient(fail=True),
            {"onnx_sha256": "b" * 64, "runtime_version": "test"},
            activate=True,
        )
    async with sessions() as session:
        assignment = await session.scalar(select(DatasetEmbeddingProfileRow))
        assert assignment is not None
        assert assignment.state == EmbeddingProfileState.FAILED
        assert assignment.is_active is False
        assert "injected provider failure" in str(assignment.last_error)
    await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_rejects_profile_metadata_collision(tmp_path) -> None:
    engine, sessions = await seeded_database(tmp_path)
    client = FakeEmbeddingClient()
    manifest = {"onnx_sha256": "b" * 64, "runtime_version": "test"}
    await backfill_embeddings(
        engine,
        sessions,
        embedding_config(tmp_path),
        "dataset-v1",
        client,
        manifest,
        activate=True,
    )
    async with sessions() as session:
        profile = await session.get(EmbeddingProfileRow, client.profile_id)
        assert profile is not None
        profile.onnx_sha256 = "c" * 64
        await session.commit()

    with pytest.raises(EmbeddingBackfillError, match="identifier collision"):
        await backfill_embeddings(
            engine,
            sessions,
            embedding_config(tmp_path),
            "dataset-v1",
            client,
            manifest,
            activate=True,
        )
    async with sessions() as session:
        assignment = await session.scalar(select(DatasetEmbeddingProfileRow))
        assert assignment is not None
        assert assignment.state == EmbeddingProfileState.ACTIVE
        assert assignment.is_active is True
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vector", "message"),
    [
        ([1.0, 0.0], "expected 3 embedding dimensions"),
        ([float("nan"), 0.0, 0.0], "non-finite"),
        ([1.0, 1.0, 0.0], "non-normalized"),
    ],
)
async def test_backfill_rejects_invalid_provider_vectors(
    tmp_path, vector: list[float], message: str
) -> None:
    engine, sessions = await seeded_database(tmp_path)
    client = FakeEmbeddingClient()

    async def invalid_documents(texts: list[str]) -> list[list[float]]:
        return [vector for _ in texts]

    client.embed_documents = invalid_documents  # type: ignore[method-assign]
    with pytest.raises(EmbeddingBackfillError, match=message):
        await backfill_embeddings(
            engine,
            sessions,
            embedding_config(tmp_path),
            "dataset-v1",
            client,
            {"onnx_sha256": "b" * 64, "runtime_version": "test"},
            activate=True,
        )
    async with sessions() as session:
        assert await session.scalar(select(func.count(ChunkEmbeddingRow.row_id))) == 0
    await engine.dispose()
