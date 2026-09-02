from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .backfill import _workbook_candidate_key, parse_meeting_date_range
from .config import Settings, load_working_groups
from .constants import ArtifactKind, DatasetState, SourceRole
from .domain import Meeting
from .ingestion.download import DownloadedArtifact
from .ingestion.pipeline import (
    create_dataset,
    ingest_document_artifact,
    ingest_tdoc_workbook,
    persist_meeting,
    persist_raw_artifact,
    update_meeting_dates,
    validate_dataset,
)
from .meeting_sources import (
    ingest_meeting_source,
    source_artifact_kind,
    source_document_state,
    source_logical_document_id,
)
from .parsers.documents import UnsafeDocumentError, UnsupportedDocumentError
from .sources.adapter import DiscoveredArtifact
from .storage.database import (
    ArtifactVersionRow,
    DatasetVersionRow,
    DocumentBlockRow,
    create_engine_and_session,
)
from .storage.object_store import LocalObjectStore


async def ingest_local_manifests(
    settings: Settings,
    manifest_paths: list[Path],
    dataset_version: str,
    meeting_ids: set[str] | None = None,
) -> dict[str, Any]:
    if settings.database.mode != "sql" or not settings.database.url.startswith("postgresql+"):
        raise ValueError("local manifest ingestion requires PostgreSQL")
    if settings.object_store.backend != "local":
        raise ValueError("local manifest ingestion requires the local object store")
    manifests = [_load_manifest(path) for path in manifest_paths]
    groups = load_working_groups()
    engine, sessions = create_engine_and_session(settings.database)
    store = LocalObjectStore(settings.object_store.local_path)
    started = time.monotonic()
    result: dict[str, Any] = {
        "dataset_version": dataset_version,
        "manifests": [str(path) for path in manifest_paths],
        "meetings": [],
    }
    try:
        async with sessions() as session:
            dataset = await session.get(DatasetVersionRow, dataset_version)
            if dataset and dataset.is_active:
                raise ValueError("an active dataset version is immutable")
            if dataset is None:
                await create_dataset(session, dataset_version)
            else:
                dataset.state = DatasetState.BUILDING
            await session.commit()

        for manifest in manifests:
            group_id = str(manifest["working_group"])
            group = groups.get(group_id)
            if group is None:
                raise ValueError(f"manifest uses unknown working group {group_id}")
            entries = list(manifest["artifacts"].values())
            selected_meetings = manifest.get("selected_meetings") or list(manifest["meetings"])
            for meeting_id in selected_meetings:
                if meeting_ids is not None and meeting_id.casefold() not in meeting_ids:
                    continue
                source_url = manifest["meetings"][meeting_id]
                meeting_entries = [
                    entry for entry in entries if entry.get("meeting_id") == meeting_id
                ]
                meeting_result = await _ingest_local_meeting(
                    settings,
                    sessions,
                    store,
                    dataset_version,
                    group_id,
                    meeting_id,
                    str(source_url),
                    meeting_entries,
                )
                result["meetings"].append(meeting_result)

        async with sessions() as session:
            result["dataset_stats"] = await validate_dataset(session, dataset_version)
            dataset = await session.get(DatasetVersionRow, dataset_version)
            assert dataset is not None
            dataset.stats = {
                **dataset.stats,
                "local_ingestion": {
                    "manifests": len(manifests),
                    "meetings": len(result["meetings"]),
                },
            }
            await session.commit()
    finally:
        await engine.dispose()
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


async def _ingest_local_meeting(
    settings: Settings,
    sessions: Any,
    store: LocalObjectStore,
    dataset_version: str,
    group_id: str,
    meeting_id: str,
    source_url: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    number, variant = _meeting_number_variant(meeting_id)
    report_entries = [entry for entry in entries if entry["kind"] == ArtifactKind.REPORT]
    final_report = any(
        "draft" not in entry["filename"].casefold()
        and "skeleton" not in entry["filename"].casefold()
        for entry in report_entries
    )
    meeting = Meeting(
        id=meeting_id,
        working_group_id=group_id,
        number=number,
        variant=variant,
        name=f"{group_id}#{number}{('-' + variant) if variant else ''}",
        source_url=source_url,
        readiness="final_ready" if final_report else "provisional_ready",
    )
    async with sessions() as session:
        await create_dataset(session, dataset_version)
        await persist_meeting(session, dataset_version, meeting)
        await session.commit()

    workbook_entries = [entry for entry in entries if entry["kind"] == ArtifactKind.TDOC_LIST]
    if not workbook_entries:
        raise ValueError(f"{meeting_id} has no TDoc index in its local manifest")
    workbook_sources = [_discovered(entry) for entry in workbook_entries]
    selected_workbook = max(workbook_sources, key=_workbook_candidate_key)
    selected_entry = next(
        entry for entry in workbook_entries if entry["url"] == selected_workbook.url
    )
    workbook_artifact = await _load_artifact(store, selected_entry)
    async with sessions() as session:
        tdocs = await ingest_tdoc_workbook(
            session,
            store,
            dataset_version,
            meeting,
            selected_entry["filename"],
            workbook_artifact,
            settings.parsers,
        )
        await session.commit()

    metadata_entries = [
        entry
        for entry in entries
        if entry["kind"] in {ArtifactKind.AGENDA, ArtifactKind.OTHER}
        or (entry["kind"] == ArtifactKind.TDOC_LIST and entry["url"] != selected_entry["url"])
    ]
    for entry in metadata_entries:
        artifact = await _load_artifact(store, entry)
        async with sessions() as session:
            await persist_raw_artifact(
                session,
                store,
                dataset_version,
                meeting,
                entry["filename"],
                artifact,
                ArtifactKind(entry["kind"]),
                ensure_parents=False,
            )
            await session.commit()

    source_entries = [
        entry
        for entry in entries
        if ArtifactKind(entry["kind"])
        in {
            ArtifactKind.REPORT,
            ArtifactKind.CHAIR_NOTES,
            ArtifactKind.POST_MEETING_DISCUSSION,
        }
    ]
    existing_statuses = await _artifact_statuses(sessions, dataset_version, meeting.id)
    enriched_sources = await _enriched_source_artifacts(
        sessions, dataset_version, meeting.id
    )
    source_results: list[dict[str, Any]] = []
    for entry in sorted(source_entries, key=lambda item: item["filename"].casefold()):
        artifact_key = (entry["url"], entry["sha256"])
        if existing_statuses.get(artifact_key) == "quarantined" or artifact_key in enriched_sources:
            source_results.append(
                {
                    "filename": entry["filename"],
                    "source_role": _entry_source_role(entry),
                    "status": "resumed",
                }
            )
            continue
        source_results.append(
            await _ingest_meeting_source_entry(
                settings, sessions, store, dataset_version, meeting, entry
            )
        )
    if not meeting.starts_on or not meeting.ends_on:
        await _recover_persisted_report_dates(sessions, dataset_version, meeting)

    document_entries = [entry for entry in entries if entry["kind"] == ArtifactKind.TDOC]
    by_document_id, duplicate_entries = _select_document_entries(document_entries)
    workbook_ids = {tdoc.id.upper() for tdoc in tdocs}
    matched_ids = sorted(workbook_ids & set(by_document_id))
    orphan_ids = sorted(set(by_document_id) - workbook_ids)
    existing_statuses = await _artifact_statuses(sessions, dataset_version, meeting.id)
    pending_ids = [
        document_id
        for document_id in matched_ids
        if existing_statuses.get(
            (by_document_id[document_id]["url"], by_document_id[document_id]["sha256"])
        )
        not in {"parsed", "quarantined", "not_applicable"}
    ]
    skipped = len(matched_ids) - len(pending_ids)
    document_results = await _ingest_document_entries(
        settings,
        sessions,
        store,
        dataset_version,
        meeting,
        by_document_id,
        pending_ids,
    )

    for entry in duplicate_entries:
        if existing_statuses.get((entry["url"], entry["sha256"])) == "not_applicable":
            continue
        artifact = await _load_artifact(store, entry)
        async with sessions() as session:
            await persist_raw_artifact(
                session,
                store,
                dataset_version,
                meeting,
                entry["filename"],
                artifact,
                ArtifactKind.TDOC,
                parse_status="not_applicable",
                parse_error="redundant source copy; canonical document body is ingested",
                ensure_parents=False,
            )
            await session.commit()

    for document_id in orphan_ids:
        entry = by_document_id[document_id]
        if existing_statuses.get((entry["url"], entry["sha256"])) == "not_applicable":
            continue
        artifact = await _load_artifact(store, entry)
        async with sessions() as session:
            await persist_raw_artifact(
                session,
                store,
                dataset_version,
                meeting,
                entry["filename"],
                artifact,
                ArtifactKind.TDOC,
                parse_status="not_applicable",
                parse_error="archive has no row in the authoritative meeting index",
                ensure_parents=False,
            )
            await session.commit()

    status_counts = Counter(item["status"] for item in document_results)
    print(
        f"{meeting.id}: {len(tdocs)} rows, {len(matched_ids)} matched archives, "
        f"{status_counts.get('ingested', 0)} ingested, {status_counts.get('failed', 0)} failed, "
        f"{status_counts.get('quarantined', 0)} quarantined, {skipped} resumed",
        flush=True,
    )
    return {
        "meeting_id": meeting.id,
        "manifest_artifacts": len(entries),
        "workbook": selected_entry["filename"],
        "workbook_rows": len(tdocs),
        "tdoc_archives": len(document_entries),
        "matched_archives": len(matched_ids),
        "missing_archives": sorted(workbook_ids - set(by_document_id)),
        "orphan_archives": orphan_ids,
        "reports": [
            result
            for result in source_results
            if result["source_role"] == SourceRole.REPORT
        ],
        "meeting_sources": source_results,
        "document_statuses": dict(status_counts),
        "resumed_documents": skipped,
    }


def _select_document_entries(
    entries: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(Path(entry["filename"]).stem.upper(), []).append(entry)
    selected: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for document_id, candidates in grouped.items():
        canonical = max(
            candidates,
            key=lambda entry: ("/docs/" in entry["url"].casefold(), entry["url"]),
        )
        selected[document_id] = canonical
        duplicates.extend(entry for entry in candidates if entry is not canonical)
    return selected, duplicates


async def _recover_persisted_report_dates(
    sessions: Any,
    dataset_version: str,
    meeting: Meeting,
) -> None:
    async with sessions() as session:
        texts = list(
            await session.scalars(
                select(DocumentBlockRow.text)
                .where(
                    DocumentBlockRow.dataset_version_id == dataset_version,
                    DocumentBlockRow.document_id.like(f"report:{meeting.id}:%"),
                )
                .order_by(DocumentBlockRow.document_id, DocumentBlockRow.block_index)
                .limit(50)
            )
        )
        starts_on, ends_on = parse_meeting_date_range(" ".join(texts))
        if starts_on and ends_on:
            meeting.starts_on = starts_on
            meeting.ends_on = ends_on
            await update_meeting_dates(
                session, dataset_version, meeting.id, starts_on, ends_on
            )
            await session.commit()


async def _ingest_document_entries(
    settings: Settings,
    sessions: Any,
    store: LocalObjectStore,
    dataset_version: str,
    meeting: Meeting,
    entries: dict[str, dict[str, Any]],
    document_ids: list[str],
) -> list[dict[str, Any]]:
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    for document_id in document_ids:
        queue.put_nowait(document_id)
    workers_count = settings.parsers.document_workers
    for _ in range(workers_count):
        queue.put_nowait(None)
    results: list[dict[str, Any]] = []
    lock = asyncio.Lock()
    started = time.monotonic()

    async def worker() -> None:
        while True:
            document_id = await queue.get()
            try:
                if document_id is None:
                    return
                entry = entries[document_id]
                try:
                    artifact = await _load_artifact(store, entry)
                    async with sessions() as session:
                        blocks, chunks = await ingest_document_artifact(
                            session,
                            store,
                            dataset_version,
                            meeting,
                            document_id,
                            entry["filename"],
                            artifact,
                            settings.chunking,
                            evidence_blocks=settings.evidence_blocks,
                            parser_config=settings.parsers,
                            ensure_parents=False,
                            assume_new=True,
                        )
                        await session.commit()
                    outcome = {
                        "tdoc_id": document_id,
                        "status": "ingested",
                        "blocks": len(blocks),
                        "chunks": len(chunks),
                    }
                except UnsafeDocumentError as exc:
                    await _record_document_failure(
                        sessions,
                        store,
                        dataset_version,
                        meeting,
                        entry,
                        "quarantined",
                        str(exc),
                    )
                    outcome = {
                        "tdoc_id": document_id,
                        "status": "quarantined",
                        "error": str(exc),
                    }
                except UnsupportedDocumentError as exc:
                    await _record_document_failure(
                        sessions,
                        store,
                        dataset_version,
                        meeting,
                        entry,
                        "not_applicable",
                        str(exc),
                    )
                    outcome = {
                        "tdoc_id": document_id,
                        "status": "not_applicable",
                        "error": str(exc),
                    }
                except Exception as exc:  # noqa: BLE001 - recorded and processing continues
                    await _record_document_failure(
                        sessions,
                        store,
                        dataset_version,
                        meeting,
                        entry,
                        "failed",
                        str(exc),
                    )
                    outcome = {
                        "tdoc_id": document_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                async with lock:
                    results.append(outcome)
                    processed = len(results)
                    if processed % 100 == 0 or processed == len(document_ids):
                        elapsed = max(time.monotonic() - started, 0.001)
                        failures = sum(item["status"] == "failed" for item in results)
                        print(
                            f"{meeting.id}: {processed}/{len(document_ids)} bodies, "
                            f"{failures} failed, {processed / elapsed:.2f} docs/s",
                            flush=True,
                        )
            finally:
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(workers_count)]
    await queue.join()
    await asyncio.gather(*workers)
    return results


async def _record_document_failure(
    sessions: Any,
    store: LocalObjectStore,
    dataset_version: str,
    meeting: Meeting,
    entry: dict[str, Any],
    status: str,
    error: str,
) -> None:
    artifact = await _load_artifact(store, entry)
    async with sessions() as session:
        await persist_raw_artifact(
            session,
            store,
            dataset_version,
            meeting,
            entry["filename"],
            artifact,
            ArtifactKind.TDOC,
            parse_status=status,
            parse_error=error,
            ensure_parents=False,
        )
        await session.commit()


async def _ingest_meeting_source_entry(
    settings: Settings,
    sessions: Any,
    store: LocalObjectStore,
    dataset_version: str,
    meeting: Meeting,
    entry: dict[str, Any],
) -> dict[str, Any]:
    artifact = await _load_artifact(store, entry)
    filename = entry["filename"]
    lower = filename.casefold()
    source_role = _entry_source_role(entry)
    if Path(lower).suffix not in {".zip", ".doc", ".docx", ".pdf", ".pptx", ".xlsx"}:
        async with sessions() as session:
            await persist_raw_artifact(
                session,
                store,
                dataset_version,
                meeting,
                filename,
                artifact,
                source_artifact_kind(source_role),
                parse_status="not_applicable",
                parse_error="report-directory entry has no supported document extension",
                ensure_parents=False,
                source_role=source_role,
                logical_document_id=source_logical_document_id(
                    meeting.id, source_role, filename
                ),
                document_state=source_document_state(source_role, filename),
            )
            await session.commit()
        return {
            "filename": filename,
            "source_role": source_role,
            "status": "not_applicable",
        }
    try:
        async with sessions() as session:
            blocks, chunks, observations = await ingest_meeting_source(
                session,
                store,
                dataset_version,
                meeting,
                filename,
                artifact,
                source_role,
                settings.chunking,
                evidence_blocks=settings.evidence_blocks,
                parser_config=settings.parsers,
            )
            if source_role == SourceRole.REPORT and (
                not meeting.starts_on or not meeting.ends_on
            ):
                starts_on, ends_on = parse_meeting_date_range(
                    " ".join(block.text for block in blocks[:50])
                )
                if starts_on and ends_on:
                    meeting.starts_on = starts_on
                    meeting.ends_on = ends_on
                    await update_meeting_dates(
                        session, dataset_version, meeting.id, starts_on, ends_on
                    )
            await session.commit()
        return {
            "filename": filename,
            "source_role": source_role,
            "status": "ingested",
            "blocks": len(blocks),
            "chunks": len(chunks),
            "observations": len(observations),
        }
    except Exception as exc:  # noqa: BLE001 - report failure remains visible in reconciliation
        async with sessions() as session:
            await persist_raw_artifact(
                session,
                store,
                dataset_version,
                meeting,
                filename,
                artifact,
                source_artifact_kind(source_role),
                parse_status="failed",
                parse_error=str(exc),
                ensure_parents=False,
                source_role=source_role,
                logical_document_id=source_logical_document_id(
                    meeting.id, source_role, filename
                ),
                document_state=source_document_state(source_role, filename),
            )
            await session.commit()
        return {
            "filename": filename,
            "source_role": source_role,
            "status": "failed",
            "error": str(exc),
        }


async def _artifact_statuses(
    sessions: Any, dataset_version: str, meeting_id: str
) -> dict[tuple[str, str], str]:
    async with sessions() as session:
        rows = await session.scalars(
            select(ArtifactVersionRow).where(
                ArtifactVersionRow.dataset_version_id == dataset_version,
                ArtifactVersionRow.meeting_id == meeting_id,
            )
        )
        return {(row.source_url, row.sha256): row.parse_status for row in rows}


async def _enriched_source_artifacts(
    sessions: Any, dataset_version: str, meeting_id: str
) -> set[tuple[str, str]]:
    async with sessions() as session:
        rows = await session.scalars(
            select(ArtifactVersionRow).where(
                ArtifactVersionRow.dataset_version_id == dataset_version,
                ArtifactVersionRow.meeting_id == meeting_id,
                ArtifactVersionRow.logical_document_id.is_not(None),
                ArtifactVersionRow.document_id.is_not(None),
                ArtifactVersionRow.source_role != SourceRole.OTHER,
            )
        )
        return {(row.source_url, row.sha256) for row in rows}


async def _load_artifact(store: LocalObjectStore, entry: dict[str, Any]) -> DownloadedArtifact:
    content = await store.get(entry["object_key"])
    digest = hashlib.sha256(content).hexdigest()
    if digest != entry["sha256"]:
        raise ValueError(f"local artifact hash mismatch for {entry['filename']}")
    return DownloadedArtifact(
        url=entry["url"],
        content=content,
        sha256=digest,
        content_type=entry.get("content_type") or "application/octet-stream",
        etag=entry.get("etag"),
        last_modified=entry.get("last_modified"),
    )


def _discovered(entry: dict[str, Any]) -> DiscoveredArtifact:
    return DiscoveredArtifact(
        kind=ArtifactKind(entry["kind"]),
        url=entry["url"],
        filename=entry["filename"],
        meeting_id=entry["meeting_id"],
        source_role=_entry_source_role(entry),
    )


def _entry_source_role(entry: dict[str, Any]) -> SourceRole:
    explicit = entry.get("source_role")
    if explicit:
        return SourceRole(explicit)
    return {
        ArtifactKind.REPORT: SourceRole.REPORT,
        ArtifactKind.CHAIR_NOTES: SourceRole.CHAIR_NOTES,
        ArtifactKind.POST_MEETING_DISCUSSION: SourceRole.POST_MEETING_DISCUSSION,
        ArtifactKind.TDOC: SourceRole.TDOC,
        ArtifactKind.TDOC_LIST: SourceRole.TDOC_LIST,
        ArtifactKind.AGENDA: SourceRole.AGENDA,
    }.get(ArtifactKind(entry["kind"]), SourceRole.OTHER)


def _meeting_number_variant(meeting_id: str) -> tuple[int, str]:
    match = re.fullmatch(r"[A-Z0-9]+-(?P<number>\d+)(?:-(?P<variant>.+))?", meeting_id)
    if not match:
        raise ValueError(f"invalid canonical meeting identifier: {meeting_id}")
    return int(match.group("number")), (match.group("variant") or "").casefold()


def _load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"local manifest root must be an object in {path}")
    if manifest.get("schema_version") != 1:
        raise ValueError(f"unsupported local manifest schema in {path}")
    failures = [
        entry["filename"]
        for entry in manifest.get("artifacts", {}).values()
        if entry.get("status") != "downloaded"
    ]
    if failures:
        raise ValueError(f"manifest {path} contains failed downloads: {failures[:5]}")
    return manifest
