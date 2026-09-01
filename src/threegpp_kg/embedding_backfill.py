from __future__ import annotations

import asyncio
import math
import re
import statistics
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .config import ModelEndpointConfig, Settings
from .constants import EmbeddingProfileState
from .models import EmbeddingClient, create_embedding_client, prepare_onnx_model
from .storage.database import (
    ChunkEmbeddingRow,
    DatasetEmbeddingProfileRow,
    DatasetVersionRow,
    EmbeddingProfileRow,
    RetrievalChunkRow,
    create_engine_and_session,
)


class EmbeddingBackfillError(RuntimeError):
    pass


async def run_embedding_backfill(
    settings: Settings,
    dataset_version_id: str,
    *,
    activate: bool = False,
    client: EmbeddingClient | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if settings.database.mode != "sql" or not settings.database.url.startswith("postgresql+"):
        raise EmbeddingBackfillError(
            "embedding backfill requires database.mode=sql with PostgreSQL"
        )
    config = settings.models.embedding
    owns_client = client is None
    if manifest is None:
        if config.provider != "onnx":
            raise EmbeddingBackfillError(
                "local backfill currently requires models.embedding.provider=onnx"
            )
        manifest = await asyncio.to_thread(prepare_onnx_model, config)
    if client is None:
        client = create_embedding_client(
            config,
            timeout_seconds=settings.models.timeout_seconds,
            retries=settings.models.retries,
        )
    engine, sessions = create_engine_and_session(settings.database)
    try:
        return await backfill_embeddings(
            engine,
            sessions,
            config,
            dataset_version_id,
            client,
            manifest,
            activate=activate,
        )
    finally:
        if owns_client and client is not None:
            await client.close()
        await engine.dispose()


async def backfill_embeddings(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    config: ModelEndpointConfig,
    dataset_version_id: str,
    client: EmbeddingClient,
    manifest: dict[str, Any],
    *,
    activate: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    async with _backfill_lock(engine, dataset_version_id, client.profile_id):
        await _initialize_profile(sessions, config, dataset_version_id, client, manifest)
        try:
            embedded_now = await _embed_missing_chunks(sessions, dataset_version_id, client, config)
            validation = await validate_embedding_profile(
                sessions, dataset_version_id, client.profile_id, client.dimensions
            )
            if not validation["valid"]:
                raise EmbeddingBackfillError(
                    "embedding profile validation failed: " + "; ".join(validation["errors"])
                )
            await _mark_validated(sessions, dataset_version_id, client.profile_id, validation)
            index_name = await create_profile_hnsw_index(
                engine, client.profile_id, client.dimensions
            )
            if activate:
                await activate_embedding_profile(sessions, dataset_version_id, client.profile_id)
        except Exception as exc:
            await _mark_failed(sessions, dataset_version_id, client.profile_id, str(exc))
            raise
    return {
        "dataset_version": dataset_version_id,
        "profile_id": client.profile_id,
        "model": client.model_name,
        "revision": client.revision,
        "dimensions": client.dimensions,
        "embedded_now": embedded_now,
        "total_chunks": validation["total_chunks"],
        "embedded_chunks": validation["embedded_chunks"],
        "coverage": validation["coverage"],
        "index": index_name,
        "activated": activate,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


@asynccontextmanager
async def _backfill_lock(
    engine: AsyncEngine, dataset_version_id: str, profile_id: str
) -> AsyncIterator[None]:
    if engine.dialect.name != "postgresql":
        yield
        return
    key = f"embedding-backfill:{dataset_version_id}:{profile_id}"
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        acquired = bool(
            await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                {"key": key},
            )
        )
        if not acquired:
            raise EmbeddingBackfillError(
                "another embedding backfill is already running for this dataset and profile"
            )
        try:
            yield
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                {"key": key},
            )


async def _initialize_profile(
    sessions: async_sessionmaker[AsyncSession],
    config: ModelEndpointConfig,
    dataset_version_id: str,
    client: EmbeddingClient,
    manifest: dict[str, Any],
) -> None:
    async with sessions() as session:
        dataset = await session.get(DatasetVersionRow, dataset_version_id)
        if dataset is None:
            raise EmbeddingBackfillError(f"dataset {dataset_version_id} does not exist")
        profile = await session.get(EmbeddingProfileRow, client.profile_id)
        if profile is None:
            profile = EmbeddingProfileRow(
                id=client.profile_id,
                provider=config.provider,
                model=client.model_name,
                revision=client.revision,
                dimensions=client.dimensions,
                pooling=config.pooling,
                normalize=config.normalize,
                query_prompt=config.query_prompt,
                document_prompt=config.document_prompt,
                onnx_sha256=str(manifest["onnx_sha256"]),
                runtime_version=str(manifest["runtime_version"]),
                created_at=datetime.now(UTC),
            )
            session.add(profile)
        else:
            expected = (
                config.provider,
                client.model_name,
                client.revision,
                client.dimensions,
                config.pooling,
                config.normalize,
                config.query_prompt,
                config.document_prompt,
                str(manifest["onnx_sha256"]),
                str(manifest["runtime_version"]),
            )
            actual = (
                profile.provider,
                profile.model,
                profile.revision,
                profile.dimensions,
                profile.pooling,
                profile.normalize,
                profile.query_prompt,
                profile.document_prompt,
                profile.onnx_sha256,
                profile.runtime_version,
            )
            if actual != expected:
                raise EmbeddingBackfillError("embedding profile identifier collision")
        assignment = await session.scalar(
            select(DatasetEmbeddingProfileRow).where(
                DatasetEmbeddingProfileRow.dataset_version_id == dataset_version_id,
                DatasetEmbeddingProfileRow.profile_id == client.profile_id,
            )
        )
        total_chunks = int(
            await session.scalar(
                select(func.count(RetrievalChunkRow.row_id)).where(
                    RetrievalChunkRow.dataset_version_id == dataset_version_id
                )
            )
            or 0
        )
        if assignment is None:
            session.add(
                DatasetEmbeddingProfileRow(
                    dataset_version_id=dataset_version_id,
                    profile_id=client.profile_id,
                    state=EmbeddingProfileState.BUILDING,
                    is_active=False,
                    total_chunks=total_chunks,
                    embedded_chunks=0,
                    created_at=datetime.now(UTC),
                )
            )
        elif not assignment.is_active:
            assignment.state = EmbeddingProfileState.BUILDING
            assignment.total_chunks = total_chunks
            assignment.last_error = None
        await session.commit()


async def _embed_missing_chunks(
    sessions: async_sessionmaker[AsyncSession],
    dataset_version_id: str,
    client: EmbeddingClient,
    config: ModelEndpointConfig,
) -> int:
    embedded_now = 0
    async with sessions() as session:
        existing = int(
            await session.scalar(
                select(func.count(ChunkEmbeddingRow.row_id)).where(
                    ChunkEmbeddingRow.dataset_version_id == dataset_version_id,
                    ChunkEmbeddingRow.profile_id == client.profile_id,
                )
            )
            or 0
        )
    while True:
        async with sessions() as session:
            rows = list(
                (
                    await session.execute(
                        select(
                            RetrievalChunkRow.id,
                            RetrievalChunkRow.text,
                            RetrievalChunkRow.token_count,
                        )
                        .outerjoin(
                            ChunkEmbeddingRow,
                            (ChunkEmbeddingRow.dataset_version_id == dataset_version_id)
                            & (ChunkEmbeddingRow.chunk_id == RetrievalChunkRow.id)
                            & (ChunkEmbeddingRow.profile_id == client.profile_id),
                        )
                        .where(
                            RetrievalChunkRow.dataset_version_id == dataset_version_id,
                            ChunkEmbeddingRow.row_id.is_(None),
                        )
                        .order_by(RetrievalChunkRow.token_count, RetrievalChunkRow.id)
                        .limit(config.batch_size)
                    )
                ).all()
            )
        if not rows:
            break
        vectors = await client.embed_documents([row.text for row in rows])
        _validate_vector_batch(vectors, len(rows), client.dimensions, config.normalize)
        async with sessions() as session:
            session.add_all(
                [
                    ChunkEmbeddingRow(
                        dataset_version_id=dataset_version_id,
                        chunk_id=row.id,
                        profile_id=client.profile_id,
                        dimensions=client.dimensions,
                        embedding=vector,
                        created_at=datetime.now(UTC),
                    )
                    for row, vector in zip(rows, vectors, strict=True)
                ]
            )
            await session.flush()
            embedded_now += len(rows)
            await session.execute(
                update(DatasetEmbeddingProfileRow)
                .where(
                    DatasetEmbeddingProfileRow.dataset_version_id == dataset_version_id,
                    DatasetEmbeddingProfileRow.profile_id == client.profile_id,
                )
                .values(embedded_chunks=existing + embedded_now)
            )
            await session.commit()
    return embedded_now


def _validate_vector_batch(
    vectors: list[list[float]], expected_count: int, dimensions: int, normalize: bool
) -> None:
    if len(vectors) != expected_count:
        raise EmbeddingBackfillError("embedding provider returned a partial batch")
    for vector in vectors:
        if len(vector) != dimensions:
            raise EmbeddingBackfillError(
                f"expected {dimensions} embedding dimensions, received {len(vector)}"
            )
        if not all(math.isfinite(value) for value in vector):
            raise EmbeddingBackfillError("embedding provider returned a non-finite vector")
        norm = math.sqrt(sum(value * value for value in vector))
        if normalize and not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3):
            raise EmbeddingBackfillError("embedding provider returned a non-normalized vector")


async def validate_embedding_profile(
    sessions: async_sessionmaker[AsyncSession],
    dataset_version_id: str,
    profile_id: str,
    dimensions: int,
) -> dict[str, Any]:
    async with sessions() as session:
        total = int(
            await session.scalar(
                select(func.count(RetrievalChunkRow.row_id)).where(
                    RetrievalChunkRow.dataset_version_id == dataset_version_id
                )
            )
            or 0
        )
        embedded = int(
            await session.scalar(
                select(func.count(ChunkEmbeddingRow.row_id)).where(
                    ChunkEmbeddingRow.dataset_version_id == dataset_version_id,
                    ChunkEmbeddingRow.profile_id == profile_id,
                )
            )
            or 0
        )
        wrong_dimensions = int(
            await session.scalar(
                select(func.count(ChunkEmbeddingRow.row_id)).where(
                    ChunkEmbeddingRow.dataset_version_id == dataset_version_id,
                    ChunkEmbeddingRow.profile_id == profile_id,
                    ChunkEmbeddingRow.dimensions != dimensions,
                )
            )
            or 0
        )
    errors: list[str] = []
    if total == 0:
        errors.append("dataset has no retrieval chunks")
    if embedded != total:
        errors.append(f"embedded {embedded} of {total} chunks")
    if wrong_dimensions:
        errors.append(f"{wrong_dimensions} vectors have incorrect dimensions")
    return {
        "valid": not errors,
        "errors": errors,
        "total_chunks": total,
        "embedded_chunks": embedded,
        "coverage": embedded / total if total else 0.0,
    }


async def _mark_validated(
    sessions: async_sessionmaker[AsyncSession],
    dataset_version_id: str,
    profile_id: str,
    validation: dict[str, Any],
) -> None:
    async with sessions() as session:
        assignment = await session.scalar(
            select(DatasetEmbeddingProfileRow).where(
                DatasetEmbeddingProfileRow.dataset_version_id == dataset_version_id,
                DatasetEmbeddingProfileRow.profile_id == profile_id,
            )
        )
        if assignment is None:
            raise EmbeddingBackfillError("embedding profile assignment disappeared")
        if not assignment.is_active:
            assignment.state = EmbeddingProfileState.VALIDATED
        assignment.total_chunks = validation["total_chunks"]
        assignment.embedded_chunks = validation["embedded_chunks"]
        assignment.last_error = None
        await session.commit()


async def _mark_failed(
    sessions: async_sessionmaker[AsyncSession],
    dataset_version_id: str,
    profile_id: str,
    error: str,
) -> None:
    async with sessions() as session:
        assignment = await session.scalar(
            select(DatasetEmbeddingProfileRow).where(
                DatasetEmbeddingProfileRow.dataset_version_id == dataset_version_id,
                DatasetEmbeddingProfileRow.profile_id == profile_id,
            )
        )
        if assignment is not None:
            if not assignment.is_active:
                assignment.state = EmbeddingProfileState.FAILED
            assignment.last_error = error[:2000]
        await session.commit()


async def activate_embedding_profile(
    sessions: async_sessionmaker[AsyncSession], dataset_version_id: str, profile_id: str
) -> None:
    async with sessions() as session:
        candidate = await session.scalar(
            select(DatasetEmbeddingProfileRow)
            .where(
                DatasetEmbeddingProfileRow.dataset_version_id == dataset_version_id,
                DatasetEmbeddingProfileRow.profile_id == profile_id,
            )
            .with_for_update()
        )
        if candidate is not None and candidate.is_active:
            return
        if candidate is None or candidate.state != EmbeddingProfileState.VALIDATED:
            raise EmbeddingBackfillError("only a validated embedding profile can be activated")
        await session.execute(
            update(DatasetEmbeddingProfileRow)
            .where(
                DatasetEmbeddingProfileRow.dataset_version_id == dataset_version_id,
                DatasetEmbeddingProfileRow.is_active.is_(True),
            )
            .values(is_active=False, state=EmbeddingProfileState.SUPERSEDED)
        )
        candidate.is_active = True
        candidate.state = EmbeddingProfileState.ACTIVE
        candidate.activated_at = datetime.now(UTC)
        await session.commit()


async def create_profile_hnsw_index(
    engine: AsyncEngine, profile_id: str, dimensions: int
) -> str | None:
    if not re.fullmatch(r"emb-[0-9a-f]{32}|legacy-[0-9a-z-]+", profile_id):
        raise EmbeddingBackfillError("invalid embedding profile identifier")
    if not 1 <= dimensions <= 2000:
        raise EmbeddingBackfillError("HNSW vector dimensions must be between 1 and 2000")
    if engine.dialect.name != "postgresql":
        return None
    index_name = f"ix_chunk_embeddings_{profile_id.replace('-', '_')[:36]}_hnsw"
    statement = (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
        "ON chunk_embeddings USING hnsw "
        f"((embedding::vector({dimensions})) vector_cosine_ops) "
        f"WHERE profile_id = '{profile_id}'"
    )
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        valid = await connection.scalar(
            text(
                "SELECT i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :index_name"
            ),
            {"index_name": index_name},
        )
        if valid is False:
            await connection.execute(text(f"DROP INDEX CONCURRENTLY {index_name}"))
        await connection.execute(text(statement))
    return index_name


async def benchmark_cached_embeddings(
    settings: Settings,
    dataset_version_id: str,
    *,
    sample_size: int = 64,
) -> dict[str, Any]:
    config = settings.models.embedding
    client = create_embedding_client(
        config,
        timeout_seconds=settings.models.timeout_seconds,
        retries=settings.models.retries,
    )
    engine, sessions = create_engine_and_session(settings.database)
    try:
        async with sessions() as session:
            texts = list(
                await session.scalars(
                    select(RetrievalChunkRow.text)
                    .where(RetrievalChunkRow.dataset_version_id == dataset_version_id)
                    .order_by(RetrievalChunkRow.token_count)
                    .limit(sample_size)
                )
            )
        if not texts:
            raise EmbeddingBackfillError("dataset has no retrieval chunks to benchmark")
        await client.warmup()
        query_latencies: list[float] = []
        for value in texts[: min(20, len(texts))]:
            started = time.perf_counter()
            await client.embed_queries([value[:500]])
            query_latencies.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        vectors = await client.embed_documents(texts)
        document_seconds = time.perf_counter() - started
        return {
            "dataset_version": dataset_version_id,
            "profile_id": client.profile_id,
            "model": client.model_name,
            "revision": client.revision,
            "dimensions": client.dimensions,
            "sample_size": len(texts),
            "query_latency_ms": {
                "p50": round(statistics.median(query_latencies), 3),
                "p95": round(_percentile(query_latencies, 0.95), 3),
            },
            "document_chunks_per_second": round(len(vectors) / document_seconds, 3),
            "all_vectors_finite": all(
                math.isfinite(value) for vector in vectors for value in vector
            ),
        }
    finally:
        await client.close()
        await engine.dispose()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]
