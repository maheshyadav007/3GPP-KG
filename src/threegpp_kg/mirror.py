from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .backfill import _discover_meeting_artifacts, _required_download, _text
from .config import Settings, WorkingGroupConfig
from .constants import ArtifactKind
from .ingestion.download import SafeDownloader
from .sources.adapter import DiscoveredArtifact, SourceAdapter
from .storage.object_store import LocalObjectStore

MANIFEST_SCHEMA_VERSION = 1


async def benchmark_download_concurrency(
    settings: Settings,
    group: WorkingGroupConfig,
    meeting_id: str,
    sample_size: int = 40,
) -> dict[str, Any]:
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    adapter = SourceAdapter(group, set(settings.security.allowed_source_hosts))
    discovery_config = settings.http.model_copy(
        update={"requests_per_second": 10.0, "max_concurrency": 10}
    )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http.timeout_seconds),
        follow_redirects=True,
        headers={"User-Agent": settings.http.user_agent},
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
    ) as client:
        downloader = SafeDownloader(
            discovery_config, set(settings.security.allowed_source_hosts), client
        )
        root = await _required_download(downloader, group.root_url)
        meetings = {item.id.casefold(): item for item in adapter.discover_meetings(_text(root))}
        meeting = meetings.get(meeting_id.casefold())
        if meeting is None:
            raise ValueError(f"meeting {meeting_id} was not found in {group.root_url}")
        by_role = await _discover_meeting_artifacts(group, adapter, downloader, meeting)
    candidates = sorted(
        {
            artifact.url: artifact
            for artifact in by_role.get("documents", [])
            if artifact.kind == ArtifactKind.TDOC
        }.values(),
        key=lambda item: item.filename.casefold(),
    )
    if not candidates:
        raise ValueError(f"meeting {meeting_id} has no downloadable TDocs")
    stride = max(len(candidates) // sample_size, 1)
    sample = candidates[::stride][:sample_size]

    trials: list[dict[str, Any]] = []
    for concurrency in (10, 20, 20, 10):
        trials.append(await _run_download_trial(settings, sample, concurrency))
    aggregate: dict[str, dict[str, Any]] = {}
    for concurrency in (10, 20):
        matching = [trial for trial in trials if trial["concurrency"] == concurrency]
        aggregate[str(concurrency)] = {
            "trials": len(matching),
            "mean_artifacts_per_second": round(
                sum(float(trial["artifacts_per_second"]) for trial in matching) / len(matching),
                3,
            ),
            "mean_mib_per_second": round(
                sum(float(trial["mib_per_second"]) for trial in matching) / len(matching), 3
            ),
            "failures": sum(int(trial["failed"]) for trial in matching),
        }
    stable = [
        (int(concurrency), metrics)
        for concurrency, metrics in aggregate.items()
        if metrics["failures"] == 0
    ]
    recommended = max(
        stable or [(10, aggregate["10"])],
        key=lambda item: float(item[1]["mean_artifacts_per_second"]),
    )[0]
    return {
        "working_group": group.id,
        "meeting_id": meeting_id,
        "sample_size": len(sample),
        "sample_urls": [artifact.url for artifact in sample],
        "trials": trials,
        "aggregate": aggregate,
        "recommended_concurrency": recommended,
    }


async def _run_download_trial(
    settings: Settings,
    artifacts: list[DiscoveredArtifact],
    concurrency: int,
) -> dict[str, Any]:
    config = settings.http.model_copy(
        update={
            "requests_per_second": float(concurrency),
            "max_concurrency": concurrency,
        }
    )
    started = time.monotonic()
    counters = {"downloaded": 0, "failed": 0, "bytes": 0}
    queue: asyncio.Queue[DiscoveredArtifact | None] = asyncio.Queue()
    for artifact in artifacts:
        queue.put_nowait(artifact)
    for _ in range(concurrency):
        queue.put_nowait(None)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(config.timeout_seconds),
        follow_redirects=True,
        headers={"User-Agent": config.user_agent},
        limits=httpx.Limits(
            max_connections=concurrency,
            max_keepalive_connections=concurrency,
        ),
    ) as client:
        downloader = SafeDownloader(config, set(settings.security.allowed_source_hosts), client)

        async def worker() -> None:
            while True:
                artifact = await queue.get()
                try:
                    if artifact is None:
                        return
                    try:
                        downloaded = await downloader.download(artifact.url)
                        if downloaded is None:
                            raise RuntimeError("unexpected not-modified response")
                        counters["downloaded"] += 1
                        counters["bytes"] += len(downloaded.content)
                    except Exception:  # noqa: BLE001 - benchmark records aggregate failures
                        counters["failed"] += 1
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
        await queue.join()
        await asyncio.gather(*workers)
    elapsed = max(time.monotonic() - started, 0.001)
    return {
        "concurrency": concurrency,
        **counters,
        "elapsed_seconds": round(elapsed, 3),
        "artifacts_per_second": round(len(artifacts) / elapsed, 3),
        "mib_per_second": round(counters["bytes"] / 1048576 / elapsed, 3),
    }


async def mirror_working_group(
    settings: Settings,
    group: WorkingGroupConfig,
    meeting_ids: list[str],
    manifest_path: Path,
) -> dict[str, Any]:
    """Download a WG corpus to local immutable storage without using the database."""
    if settings.object_store.backend != "local":
        raise ValueError("corpus mirroring requires the local object store")
    if not meeting_ids:
        raise ValueError("at least one meeting is required")

    store = LocalObjectStore(settings.object_store.local_path)
    adapter = SourceAdapter(group, set(settings.security.allowed_source_hosts))
    limits = httpx.Limits(
        max_connections=settings.http.max_concurrency,
        max_keepalive_connections=settings.http.max_concurrency,
    )
    timeout = httpx.Timeout(settings.http.timeout_seconds)
    headers = {"User-Agent": settings.http.user_agent}
    started = time.monotonic()
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
        limits=limits,
    ) as client:
        downloader = SafeDownloader(
            settings.http, set(settings.security.allowed_source_hosts), client
        )
        root = await _required_download(downloader, group.root_url)
        discovered = {item.id.casefold(): item for item in adapter.discover_meetings(_text(root))}
        artifacts: dict[str, DiscoveredArtifact] = {}
        meeting_sources: dict[str, str] = {}
        for meeting_id in meeting_ids:
            meeting = discovered.get(meeting_id.casefold())
            if meeting is None:
                raise ValueError(f"meeting {meeting_id} was not found in {group.root_url}")
            meeting_sources[meeting.id] = meeting.url
            by_role = await _discover_meeting_artifacts(group, adapter, downloader, meeting)
            for role, role_artifacts in by_role.items():
                for artifact in role_artifacts:
                    if artifact.kind != ArtifactKind.OTHER or role == "invitations":
                        artifacts[artifact.url] = artifact

        manifest = _load_manifest(manifest_path, group.id)
        manifest["meetings"].update(meeting_sources)
        manifest["selected_meetings"] = list(meeting_sources)
        entries: dict[str, dict[str, Any]] = manifest["artifacts"]
        pending: list[DiscoveredArtifact] = []
        skipped = 0
        for artifact in artifacts.values():
            existing = entries.get(artifact.url)
            if (
                existing
                and existing.get("status") == "downloaded"
                and existing.get("object_key")
                and await store.exists(existing["object_key"])
            ):
                skipped += 1
            else:
                pending.append(artifact)

        counters = {"downloaded": 0, "failed": 0, "bytes": 0, "processed": 0}
        lock = asyncio.Lock()
        queue: asyncio.Queue[DiscoveredArtifact | None] = asyncio.Queue()
        for artifact in pending:
            queue.put_nowait(artifact)
        for _ in range(settings.http.max_concurrency):
            queue.put_nowait(None)

        async def worker() -> None:
            while True:
                artifact = await queue.get()
                try:
                    if artifact is None:
                        return
                    try:
                        downloaded = await downloader.download(artifact.url)
                        if downloaded is None:
                            raise RuntimeError(
                                "unexpected not-modified response without validators"
                            )
                        object_key = await store.put(
                            downloaded.sha256,
                            artifact.filename,
                            downloaded.content,
                            downloaded.content_type,
                        )
                        entry = {
                            "url": artifact.url,
                            "meeting_id": artifact.meeting_id,
                            "filename": artifact.filename,
                            "kind": artifact.kind.value,
                            "source_role": artifact.source_role.value,
                            "status": "downloaded",
                            "sha256": downloaded.sha256,
                            "size": len(downloaded.content),
                            "content_type": downloaded.content_type,
                            "etag": downloaded.etag,
                            "last_modified": downloaded.last_modified,
                            "object_key": object_key,
                            "downloaded_at": datetime.now(UTC).isoformat(),
                        }
                        async with lock:
                            entries[artifact.url] = entry
                            counters["downloaded"] += 1
                            counters["bytes"] += len(downloaded.content)
                    except Exception as exc:  # noqa: BLE001 - failure is checkpointed for retry
                        async with lock:
                            entries[artifact.url] = {
                                "url": artifact.url,
                                "meeting_id": artifact.meeting_id,
                                "filename": artifact.filename,
                                "kind": artifact.kind.value,
                                "source_role": artifact.source_role.value,
                                "status": "failed",
                                "error": str(exc),
                                "failed_at": datetime.now(UTC).isoformat(),
                            }
                            counters["failed"] += 1
                    async with lock:
                        counters["processed"] += 1
                        processed = counters["processed"]
                        if processed % 25 == 0 or processed == len(pending):
                            manifest["updated_at"] = datetime.now(UTC).isoformat()
                            manifest["settings"] = _download_settings(settings)
                            await asyncio.to_thread(_write_manifest, manifest_path, manifest)
                        if processed % 100 == 0 or processed == len(pending):
                            elapsed = max(time.monotonic() - started, 0.001)
                            print(
                                f"{group.id}: {processed}/{len(pending)} processed, "
                                f"{counters['failed']} failed, "
                                f"{counters['bytes'] / 1048576:.1f} MiB, "
                                f"{processed / elapsed:.2f} artifacts/s",
                                flush=True,
                            )
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(settings.http.max_concurrency)]
        await queue.join()
        await asyncio.gather(*workers)

    elapsed = time.monotonic() - started
    manifest["updated_at"] = datetime.now(UTC).isoformat()
    manifest["settings"] = _download_settings(settings)
    manifest["summary"] = {
        "discovered": len(artifacts),
        "skipped_existing": skipped,
        **counters,
        "elapsed_seconds": round(elapsed, 3),
    }
    await asyncio.to_thread(_write_manifest, manifest_path, manifest)
    return {
        "working_group": group.id,
        "meeting_ids": meeting_ids,
        "manifest": str(manifest_path),
        **manifest["summary"],
    }


def _load_manifest(path: Path, working_group: str) -> dict[str, Any]:
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"download manifest root must be an object in {path}")
        if loaded.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported download manifest schema in {path}")
        if loaded.get("working_group") != working_group:
            raise ValueError(f"download manifest {path} belongs to another working group")
        return loaded
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "working_group": working_group,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "settings": {},
        "meetings": {},
        "selected_meetings": [],
        "artifacts": {},
        "summary": {},
    }


def _download_settings(settings: Settings) -> dict[str, Any]:
    return {
        "requests_per_second": settings.http.requests_per_second,
        "max_concurrency": settings.http.max_concurrency,
        "timeout_seconds": settings.http.timeout_seconds,
        "retries": settings.http.retries,
        "object_store": str(settings.object_store.local_path),
    }


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
