from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader
from sqlalchemy import func, select

from .config import Settings, WorkingGroupConfig
from .constants import ArtifactKind, Conclusion, DatasetState, EvidenceAuthority
from .domain import Meeting
from .ingestion.download import DownloadedArtifact, SafeDownloader
from .ingestion.pipeline import (
    ingest_document_artifact,
    ingest_tdoc_workbook,
    persist_raw_artifact,
    update_meeting_dates,
    validate_dataset,
)
from .parsers.documents import UnsafeDocumentError
from .publisher import activate_dataset
from .sources.adapter import DiscoveredArtifact, DiscoveredMeeting, SourceAdapter
from .storage.database import (
    ArtifactVersionRow,
    DatasetVersionRow,
    TDocRow,
    create_engine_and_session,
)
from .storage.object_store import LocalObjectStore


@dataclass(frozen=True, slots=True)
class BackfillRequest:
    working_group: str
    meeting_ids: list[str]
    dataset_version: str
    last_k_meetings: int | None = None
    document_limit: int = 0
    include_report: bool = True
    activate: bool = False


class BackfillError(RuntimeError):
    pass


async def run_backfill(
    settings: Settings,
    group: WorkingGroupConfig,
    request: BackfillRequest,
) -> dict[str, Any]:
    if request.working_group != group.id:
        raise BackfillError("working-group request does not match its adapter configuration")
    if request.document_limit < -1:
        raise BackfillError("document_limit must be -1 (all), 0 (none), or a positive count")
    if settings.object_store.backend != "local":
        raise BackfillError("this backfill command currently requires the local object store")
    if settings.database.mode != "sql" or not settings.database.url.startswith("postgresql+"):
        raise BackfillError("backfill requires database.mode=sql with PostgreSQL")

    engine, sessions = create_engine_and_session(settings.database)
    object_store = LocalObjectStore(settings.object_store.local_path)
    adapter = SourceAdapter(group, set(settings.security.allowed_source_hosts))
    timeout = httpx.Timeout(settings.http.timeout_seconds)
    headers = {"User-Agent": settings.http.user_agent}
    result: dict[str, Any] = {
        "dataset_version": request.dataset_version,
        "working_group": group.id,
        "meetings": [],
        "activated": False,
    }
    try:
        async with sessions() as session:
            existing = await session.get(DatasetVersionRow, request.dataset_version)
            if existing and existing.is_active:
                raise BackfillError("an active dataset version is immutable")
            if existing:
                existing.state = DatasetState.BUILDING
                await session.commit()
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=headers
        ) as client:
            downloader = SafeDownloader(
                settings.http, set(settings.security.allowed_source_hosts), client
            )
            root = await _required_download(downloader, group.root_url)
            discovered = {
                item.id.casefold(): item for item in adapter.discover_meetings(_text(root))
            }
            meeting_ids = request.meeting_ids
            if request.last_k_meetings is not None:
                if meeting_ids:
                    raise BackfillError("meeting_ids and last_k_meetings are mutually exclusive")
                if request.last_k_meetings < 1:
                    raise BackfillError("last_k_meetings must be positive")
                meeting_ids = await _latest_completed_meeting_ids(
                    group,
                    adapter,
                    downloader,
                    list(discovered.values()),
                    request.last_k_meetings,
                )
            if not meeting_ids:
                raise BackfillError("at least one meeting or last_k_meetings is required")
            result["selected_meeting_ids"] = meeting_ids
            for meeting_id in meeting_ids:
                source_meeting = discovered.get(meeting_id.casefold())
                if source_meeting is None:
                    raise BackfillError(f"meeting {meeting_id} was not found in {group.root_url}")
                meeting_result = await _ingest_meeting(
                    settings,
                    group,
                    adapter,
                    downloader,
                    sessions,
                    object_store,
                    source_meeting,
                    request,
                )
                result["meetings"].append(meeting_result)

        if request.activate:
            _validate_activation_readiness(result["meetings"])
        async with sessions() as session:
            stats = await validate_dataset(session, request.dataset_version)
            dataset = await session.get(DatasetVersionRow, request.dataset_version)
            assert dataset is not None
            previous_backfill = dataset.stats.get("backfill", []) if dataset.stats else []
            current_ids = {meeting["meeting_id"] for meeting in result["meetings"]}
            combined_backfill = [
                meeting
                for meeting in previous_backfill
                if meeting.get("meeting_id") not in current_ids
            ] + result["meetings"]
            dataset.stats = {**stats, "backfill": combined_backfill}
            await session.commit()
        result["stats"] = stats
        if request.activate:
            async with sessions() as session:
                await activate_dataset(session, request.dataset_version)
                await session.commit()
            result["activated"] = True
        return result
    finally:
        await engine.dispose()


async def _latest_completed_meeting_ids(
    group: WorkingGroupConfig,
    adapter: SourceAdapter,
    downloader: SafeDownloader,
    meetings: list[DiscoveredMeeting],
    limit: int,
) -> list[str]:
    selected: list[str] = []
    for meeting in sorted(meetings, key=lambda item: (item.number, item.variant), reverse=True):
        artifacts = await _discover_meeting_artifacts(group, adapter, downloader, meeting)
        has_tdoc_list = any(
            artifact.kind == ArtifactKind.TDOC_LIST
            for role in ("tdoc_lists", "documents")
            for artifact in artifacts.get(role, [])
        )
        has_tdoc_archives = any(
            artifact.kind == ArtifactKind.TDOC for artifact in artifacts.get("documents", [])
        )
        if has_tdoc_list and has_tdoc_archives:
            selected.append(meeting.id)
            if len(selected) == limit:
                return list(reversed(selected))
    raise BackfillError(
        f"only {len(selected)} completed {group.id} meetings were found; requested {limit}"
    )


async def _ingest_meeting(
    settings: Settings,
    group: WorkingGroupConfig,
    adapter: SourceAdapter,
    downloader: SafeDownloader,
    sessions: Any,
    object_store: LocalObjectStore,
    source_meeting: DiscoveredMeeting,
    request: BackfillRequest,
) -> dict[str, Any]:
    artifacts = await _discover_meeting_artifacts(group, adapter, downloader, source_meeting)
    workbook_candidates = _deduplicate(
        artifact
        for role in ("tdoc_lists", "documents")
        for artifact in artifacts.get(role, [])
        if artifact.kind == ArtifactKind.TDOC_LIST
    )
    if not workbook_candidates:
        raise BackfillError(f"no TDoc workbook found for {source_meeting.id}")
    workbook = max(workbook_candidates, key=_workbook_candidate_key)

    starts_on, ends_on = await _discover_meeting_dates(downloader, artifacts.get("invitations", []))
    report = _select_report(artifacts.get("reports", [])) if request.include_report else None
    meeting = Meeting(
        id=source_meeting.id,
        working_group_id=group.id,
        number=source_meeting.number,
        variant=source_meeting.variant,
        name=f"{group.id}#{source_meeting.number}{source_meeting.variant}",
        source_url=source_meeting.url,
        starts_on=starts_on,
        ends_on=ends_on,
        readiness=(
            "final_ready"
            if report and "draft" not in report.filename.lower()
            else "provisional_ready"
        ),
    )

    workbook_download = await _required_download(downloader, workbook.url)
    async with sessions() as session:
        tdocs = await ingest_tdoc_workbook(
            session,
            object_store,
            request.dataset_version,
            meeting,
            workbook.filename,
            workbook_download,
            settings.parsers,
        )
        if meeting.starts_on and meeting.ends_on:
            await update_meeting_dates(
                session,
                request.dataset_version,
                meeting.id,
                meeting.starts_on,
                meeting.ends_on,
            )
        await session.commit()

    agenda_candidates = [
        item for item in artifacts.get("agenda", []) if item.kind == ArtifactKind.AGENDA
    ]
    if agenda_candidates:
        agenda = max(agenda_candidates, key=lambda item: _natural_key(item.filename))
        agenda_download = await _required_download(downloader, agenda.url)
        async with sessions() as session:
            await persist_raw_artifact(
                session,
                object_store,
                request.dataset_version,
                meeting,
                agenda.filename,
                agenda_download,
                ArtifactKind.AGENDA,
            )
            await session.commit()

    report_status: dict[str, Any] | None = None
    if report:
        try:
            report_download = await _required_download(downloader, report.url)
            authority = (
                EvidenceAuthority.DRAFT_REPORT
                if "draft" in report.filename.lower() or "skeleton" in report.filename.lower()
                else EvidenceAuthority.FINAL_REPORT
            )
            async with sessions() as session:
                blocks, chunks = await ingest_document_artifact(
                    session,
                    object_store,
                    request.dataset_version,
                    meeting,
                    f"report:{meeting.id}",
                    report.filename,
                    report_download,
                    settings.chunking,
                    evidence_blocks=settings.evidence_blocks,
                    parser_config=settings.parsers,
                    kind=ArtifactKind.REPORT,
                    authority=authority,
                )
                date_source: str | None = None
                if not meeting.starts_on or not meeting.ends_on:
                    report_start, report_end = parse_meeting_date_range(
                        " ".join(block.text for block in blocks[:50])
                    )
                    if report_start and report_end:
                        await update_meeting_dates(
                            session,
                            request.dataset_version,
                            meeting.id,
                            report_start,
                            report_end,
                        )
                        meeting.starts_on = report_start
                        meeting.ends_on = report_end
                        date_source = "report"
                await session.commit()
            report_status = {
                "filename": report.filename,
                "blocks": len(blocks),
                "chunks": len(chunks),
                "authority": authority.value,
                "date_source": date_source,
            }
        except Exception as exc:  # noqa: BLE001 - recorded for resumable source processing
            report_status = {"filename": report.filename, "error": str(exc)}

    document_artifacts = _deduplicate(
        artifact
        for artifact in artifacts.get("documents", [])
        if artifact.kind == ArtifactKind.TDOC
    )
    by_document_id = {Path(item.filename).stem.upper(): item for item in document_artifacts}
    workbook_ids = [tdoc.id.upper() for tdoc in tdocs]
    selected_ids = [identifier for identifier in workbook_ids if identifier in by_document_id]
    if request.document_limit == 0:
        selected_ids = []
    elif request.document_limit > 0:
        selected_ids = selected_ids[: request.document_limit]
    document_results: list[dict[str, Any]] = []
    batch_size = settings.http.max_concurrency
    batches = [
        selected_ids[offset : offset + batch_size]
        for offset in range(0, len(selected_ids), batch_size)
    ]
    download_task = (
        asyncio.create_task(
            _download_document_batch(
                downloader,
                sessions,
                request.dataset_version,
                by_document_id,
                batches[0],
            )
        )
        if batches
        else None
    )
    document_workers = asyncio.Semaphore(settings.parsers.document_workers)
    try:
        for batch_index, batch in enumerate(batches):
            assert download_task is not None
            downloads = await download_task
            download_task = (
                asyncio.create_task(
                    _download_document_batch(
                        downloader,
                        sessions,
                        request.dataset_version,
                        by_document_id,
                        batches[batch_index + 1],
                    )
                )
                if batch_index + 1 < len(batches)
                else None
            )
            document_results.extend(
                await asyncio.gather(
                    *(
                        _process_document_outcome(
                            settings,
                            sessions,
                            object_store,
                            request.dataset_version,
                            meeting,
                            document_id,
                            by_document_id[document_id],
                            outcome,
                            document_workers,
                        )
                        for document_id, outcome in zip(batch, downloads, strict=True)
                    )
                )
            )
    finally:
        if download_task is not None and not download_task.done():
            download_task.cancel()
            await asyncio.gather(download_task, return_exceptions=True)

    archive_ids = set(by_document_id)
    row_ids = set(workbook_ids)
    no_archive_expected = {
        Conclusion.RESERVED,
        Conclusion.WITHDRAWN,
        Conclusion.REVISED,
        Conclusion.NOT_PURSUED,
    }
    actionable_ids = {tdoc.id.upper() for tdoc in tdocs if tdoc.status not in no_archive_expected}
    missing_statuses: dict[str, int] = {}
    for tdoc in tdocs:
        if tdoc.id.upper() not in archive_ids:
            missing_statuses[tdoc.status.value] = missing_statuses.get(tdoc.status.value, 0) + 1
    async with sessions() as session:
        persisted = int(
            await session.scalar(
                select(func.count(TDocRow.row_id)).where(
                    TDocRow.dataset_version_id == request.dataset_version,
                    TDocRow.meeting_id == meeting.id,
                )
            )
            or 0
        )
    return {
        "meeting_id": meeting.id,
        "source_url": meeting.source_url,
        "starts_on": meeting.starts_on.isoformat() if meeting.starts_on else None,
        "ends_on": meeting.ends_on.isoformat() if meeting.ends_on else None,
        "workbook": workbook.filename,
        "workbook_rows": len(tdocs),
        "persisted_tdocs": persisted,
        "tdoc_archives": len(archive_ids),
        "rows_with_archive": len(row_ids & archive_ids),
        "rows_missing_archive": sorted(row_ids - archive_ids),
        "missing_archive_by_status": missing_statuses,
        "archives_without_row": sorted(archive_ids - row_ids),
        "row_persistence_ratio": persisted / len(tdocs) if tdocs else 0,
        "archive_match_ratio": len(row_ids & archive_ids) / len(row_ids) if row_ids else 0,
        "actionable_archive_match_ratio": (
            len(actionable_ids & archive_ids) / len(actionable_ids) if actionable_ids else 1.0
        ),
        "report": report_status,
        "document_results": document_results,
    }


async def _discover_meeting_artifacts(
    group: WorkingGroupConfig,
    adapter: SourceAdapter,
    downloader: SafeDownloader,
    meeting: DiscoveredMeeting,
) -> dict[str, list[DiscoveredArtifact]]:
    root = await _required_download(downloader, meeting.url)
    root_html = _text(root)
    available = _directory_names(root_html)
    listing_cache: dict[str, str] = {meeting.url: root_html}
    found: dict[str, list[DiscoveredArtifact]] = {}
    for role, candidates in group.directories.items():
        role_artifacts: list[DiscoveredArtifact] = []
        for candidate in candidates:
            if candidate != "." and candidate.casefold() not in available:
                continue
            listing_url = meeting.url if candidate == "." else urljoin(meeting.url, candidate + "/")
            if listing_url not in listing_cache:
                listing_cache[listing_url] = _text(
                    await _required_download(downloader, listing_url)
                )
            role_artifacts.extend(
                adapter.discover_artifacts(
                    listing_cache[listing_url], listing_url, meeting.id, role
                )
            )
        found[role] = _deduplicate(role_artifacts)
    return found


async def _discover_meeting_dates(
    downloader: SafeDownloader,
    invitations: list[DiscoveredArtifact],
) -> tuple[date | None, date | None]:
    candidates = [item for item in invitations if item.filename.lower().endswith(".pdf")]
    if not candidates:
        return None, None
    invitation = max(candidates, key=lambda item: _natural_key(item.filename))
    downloaded = await _required_download(downloader, invitation.url)
    try:
        reader = PdfReader(io.BytesIO(downloaded.content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:3])
    except Exception:  # noqa: BLE001 - dates are optional metadata
        return None, None
    return parse_meeting_date_range(text)


def parse_meeting_date_range(text: str) -> tuple[date | None, date | None]:
    compact = " ".join(text.split())
    numeric = re.search(
        r"(?P<start_day>\d{1,2})/(?P<start_month>\d{1,2})/(?P<start_year>\d{4})"
        r"\s+to\s+"
        r"(?P<end_day>\d{1,2})/(?P<end_month>\d{1,2})/(?P<end_year>\d{4})",
        compact,
        flags=re.IGNORECASE,
    )
    if numeric:
        try:
            return (
                date(
                    int(numeric.group("start_year")),
                    int(numeric.group("start_month")),
                    int(numeric.group("start_day")),
                ),
                date(
                    int(numeric.group("end_year")),
                    int(numeric.group("end_month")),
                    int(numeric.group("end_day")),
                ),
            )
        except ValueError:
            pass
    cross_month = re.search(
        r"(?P<start_day>\d{1,2})\s+(?P<start_month>[A-Za-z]+)\s*[-–]\s*"
        r"(?P<end_day>\d{1,2})\s+(?P<end_month>[A-Za-z]+)\s+(?P<year>\d{4})",
        compact,
        flags=re.IGNORECASE,
    )
    if cross_month:
        try:
            year = int(cross_month.group("year"))
            start_month = datetime.strptime(cross_month.group("start_month")[:3], "%b").month
            end_month = datetime.strptime(cross_month.group("end_month")[:3], "%b").month
            return (
                date(year, start_month, int(cross_month.group("start_day"))),
                date(year, end_month, int(cross_month.group("end_day"))),
            )
        except ValueError:
            pass
    tdoc_year = re.search(
        r"\b[A-Z]\d-(?P<year>\d{2})[0-9X]+.*?"
        r"(?P<month>[A-Za-z]{3,})\.?\s+(?P<start>\d{1,2})(?:st|nd|rd|th)?"
        r"\s*[-–]\s*(?P<end>\d{1,2})(?:st|nd|rd|th)?",
        compact,
        flags=re.IGNORECASE,
    )
    if tdoc_year:
        try:
            year = 2000 + int(tdoc_year.group("year"))
            month = datetime.strptime(tdoc_year.group("month")[:3], "%b").month
            return (
                date(year, month, int(tdoc_year.group("start"))),
                date(year, month, int(tdoc_year.group("end"))),
            )
        except ValueError:
            pass
    patterns = (
        r"from\s+(?:Monday\s+)?(?P<start>\d{1,2})\s+to\s+(?:Friday\s+)?"
        r"(?P<end>\d{1,2})\s+(?:of\s+)?(?P<month>[A-Za-z]+),?\s+(?P<year>\d{4})",
        r"(?P<start>\d{1,2})\s*[-–]\s*(?P<end>\d{1,2})\s+"
        r"(?P<month>[A-Za-z]+),?\s+(?P<year>\d{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, compact, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            month = datetime.strptime(match.group("month")[:3], "%b").month
            year = int(match.group("year"))
            return date(year, month, int(match.group("start"))), date(
                year, month, int(match.group("end"))
            )
        except ValueError:
            continue
    return None, None


def _select_report(artifacts: list[DiscoveredArtifact]) -> DiscoveredArtifact | None:
    archives = [
        item for item in artifacts if item.filename.lower().endswith((".zip", ".docx", ".pdf"))
    ]
    if not archives:
        return None
    substantive = [item for item in archives if "skeleton" not in item.filename.lower()]
    return max(substantive or archives, key=lambda item: _natural_key(item.filename))


def _workbook_candidate_key(artifact: DiscoveredArtifact) -> tuple[int, int, tuple[Any, ...]]:
    filename = artifact.filename.casefold()
    extension_priority = 2 if filename.endswith(".xlsx") else 1 if filename.endswith(".zip") else 0
    name_priority = 2 if "index" in filename else 1 if "tdoc" in filename else 0
    return extension_priority, name_priority, _natural_key(filename)


async def _conditional_download(
    downloader: SafeDownloader,
    sessions: Any,
    dataset_version: str,
    url: str,
) -> tuple[DownloadedArtifact | None, str | None]:
    async with sessions() as session:
        previous = await session.scalar(
            select(ArtifactVersionRow)
            .where(
                ArtifactVersionRow.dataset_version_id == dataset_version,
                ArtifactVersionRow.source_url == url,
            )
            .order_by(ArtifactVersionRow.observed_at.desc())
            .limit(1)
        )
    downloaded = await downloader.download(
        url,
        etag=previous.etag if previous and previous.parse_status != "failed" else None,
        last_modified=(
            previous.last_modified if previous and previous.parse_status != "failed" else None
        ),
    )
    return downloaded, previous.parse_status if previous else None


async def _process_document_outcome(
    settings: Settings,
    sessions: Any,
    object_store: LocalObjectStore,
    dataset_version: str,
    meeting: Meeting,
    document_id: str,
    source: DiscoveredArtifact,
    outcome: tuple[DownloadedArtifact | None, str | None] | BaseException,
    workers: asyncio.Semaphore,
) -> dict[str, Any]:
    if isinstance(outcome, Exception):
        return {"tdoc_id": document_id, "status": "failed", "error": str(outcome)}
    if isinstance(outcome, BaseException):
        raise outcome
    downloaded, previous_status = outcome
    if downloaded is None:
        status = "quarantined" if previous_status == "quarantined" else "unchanged"
        return {"tdoc_id": document_id, "status": status}

    async with workers:
        try:
            async with sessions() as session:
                blocks, chunks = await ingest_document_artifact(
                    session,
                    object_store,
                    dataset_version,
                    meeting,
                    document_id,
                    source.filename,
                    downloaded,
                    settings.chunking,
                    evidence_blocks=settings.evidence_blocks,
                    parser_config=settings.parsers,
                )
                await session.commit()
            return {
                "tdoc_id": document_id,
                "status": "ingested",
                "blocks": len(blocks),
                "chunks": len(chunks),
            }
        except UnsafeDocumentError as exc:
            async with sessions() as session:
                await persist_raw_artifact(
                    session,
                    object_store,
                    dataset_version,
                    meeting,
                    source.filename,
                    downloaded,
                    ArtifactKind.TDOC,
                    parse_status="quarantined",
                    parse_error=str(exc),
                )
                await session.commit()
            return {
                "tdoc_id": document_id,
                "status": "quarantined",
                "error": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 - continue and expose each failed source
            async with sessions() as session:
                await persist_raw_artifact(
                    session,
                    object_store,
                    dataset_version,
                    meeting,
                    source.filename,
                    downloaded,
                    ArtifactKind.TDOC,
                    parse_status="failed",
                    parse_error=str(exc),
                )
                await session.commit()
            return {"tdoc_id": document_id, "status": "failed", "error": str(exc)}


async def _download_document_batch(
    downloader: SafeDownloader,
    sessions: Any,
    dataset_version: str,
    documents: dict[str, DiscoveredArtifact],
    document_ids: list[str],
) -> list[tuple[DownloadedArtifact | None, str | None] | BaseException]:
    outcomes = await asyncio.gather(
        *(
            _conditional_download(
                downloader,
                sessions,
                dataset_version,
                documents[document_id].url,
            )
            for document_id in document_ids
        ),
        return_exceptions=True,
    )
    return list(outcomes)


async def _required_download(downloader: SafeDownloader, url: str) -> DownloadedArtifact:
    artifact = await downloader.download(url)
    if artifact is None:
        raise BackfillError(
            f"source unexpectedly returned not-modified without a prior artifact: {url}"
        )
    return artifact


def _directory_names(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    return {
        anchor.get_text(strip=True).rstrip("/").casefold()
        for anchor in soup.find_all("a", href=True)
        if anchor.get_text(strip=True)
    }


def _text(artifact: DownloadedArtifact) -> str:
    return artifact.content.decode("utf-8", errors="replace")


def _deduplicate(artifacts: Any) -> list[DiscoveredArtifact]:
    return list({item.url: item for item in artifacts}.values())


def _natural_key(value: str) -> tuple[Any, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)
    )


def _validate_activation_readiness(meetings: list[dict[str, Any]]) -> None:
    errors: list[str] = []
    for meeting in meetings:
        meeting_id = meeting["meeting_id"]
        if meeting["row_persistence_ratio"] < 0.995:
            errors.append(f"{meeting_id} persisted fewer than 99.5% of workbook rows")
        if meeting["actionable_archive_match_ratio"] < 0.995:
            errors.append(f"{meeting_id} matched fewer than 99.5% of actionable archives")
        report = meeting.get("report")
        if report and report.get("error"):
            errors.append(f"{meeting_id} report parsing failed")
        failed_documents = [
            item["tdoc_id"] for item in meeting["document_results"] if item["status"] == "failed"
        ]
        if failed_documents:
            errors.append(f"{meeting_id} failed TDoc bodies: {failed_documents[:5]}")
    if errors:
        raise BackfillError("; ".join(errors))
