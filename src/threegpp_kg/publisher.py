from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .constants import (
    MIN_DATASET_INGESTION_COVERAGE,
    ArtifactKind,
    Conclusion,
    DatasetState,
)
from .storage.database import (
    ArtifactVersionRow,
    DatasetVersionRow,
    DocumentBlockRow,
    MeetingRow,
    TDocRow,
)


async def assess_activation_readiness(
    session: AsyncSession, dataset_version_id: str
) -> dict[str, Any]:
    dataset = await session.get(DatasetVersionRow, dataset_version_id)
    if dataset is None:
        raise ValueError(f"dataset version {dataset_version_id} does not exist")

    meeting_count = await _count(
        session,
        select(func.count(MeetingRow.row_id)).where(
            MeetingRow.dataset_version_id == dataset_version_id
        ),
    )
    undated_meetings = await _count(
        session,
        select(func.count(MeetingRow.row_id)).where(
            MeetingRow.dataset_version_id == dataset_version_id,
            (MeetingRow.starts_on.is_(None) | MeetingRow.ends_on.is_(None)),
        ),
    )
    report_meetings = await _count(
        session,
        select(func.count(func.distinct(ArtifactVersionRow.meeting_id))).where(
            ArtifactVersionRow.dataset_version_id == dataset_version_id,
            ArtifactVersionRow.kind == ArtifactKind.REPORT,
            ArtifactVersionRow.parse_status == "parsed",
        ),
    )
    tdoc_count = await _count(
        session,
        select(func.count(TDocRow.row_id)).where(TDocRow.dataset_version_id == dataset_version_id),
    )
    excluded_statuses = (
        Conclusion.RESERVED,
        Conclusion.WITHDRAWN,
        Conclusion.REVISED,
        Conclusion.NOT_PURSUED,
    )
    actionable_ids = set(
        await session.scalars(
            select(TDocRow.id).where(
                TDocRow.dataset_version_id == dataset_version_id,
                TDocRow.status.not_in(excluded_statuses),
            )
        )
    )
    artifact_rows = list(
        await session.scalars(
            select(ArtifactVersionRow).where(
                ArtifactVersionRow.dataset_version_id == dataset_version_id,
                ArtifactVersionRow.kind == ArtifactKind.TDOC,
            )
        )
    )
    artifact_ids = {_tdoc_id(row.filename) for row in artifact_rows}
    quarantined_ids = {
        _tdoc_id(row.filename) for row in artifact_rows if row.parse_status == "quarantined"
    }
    invalid_artifacts = await _count(
        session,
        select(func.count(ArtifactVersionRow.id)).where(
            ArtifactVersionRow.dataset_version_id == dataset_version_id,
            ArtifactVersionRow.parse_status.not_in(("parsed", "quarantined", "not_applicable")),
        ),
    )
    parsed_ids = set(
        await session.scalars(
            select(DocumentBlockRow.document_id)
            .where(DocumentBlockRow.dataset_version_id == dataset_version_id)
            .distinct()
        )
    )

    missing_archive_ids = sorted(actionable_ids - artifact_ids)
    handled_ids = parsed_ids | quarantined_ids
    missing_body_ids = sorted(actionable_ids - handled_ids)
    actionable_tdocs = len(actionable_ids)
    parsed_body_documents = len(parsed_ids & actionable_ids)
    archive_coverage = _ratio(actionable_tdocs - len(missing_archive_ids), actionable_tdocs)
    body_coverage = _ratio(actionable_tdocs - len(missing_body_ids), actionable_tdocs)
    errors: list[str] = []
    warnings: list[str] = []
    if dataset.state != DatasetState.VALIDATED:
        errors.append("dataset state must be validated")
    if meeting_count == 0 or tdoc_count == 0:
        errors.append("dataset must contain meetings and TDocs")
    if undated_meetings:
        errors.append(f"{undated_meetings} meetings have incomplete dates")
    if report_meetings != meeting_count:
        errors.append(f"reports cover {report_meetings} of {meeting_count} meetings")
    if invalid_artifacts:
        errors.append(f"{invalid_artifacts} artifacts have an invalid parse status")
    if archive_coverage < MIN_DATASET_INGESTION_COVERAGE:
        errors.append(
            f"actionable TDoc archive coverage is {archive_coverage:.3%}; "
            f"missing {missing_archive_ids[:5]}"
        )
    if body_coverage < MIN_DATASET_INGESTION_COVERAGE:
        errors.append(
            f"parsed or quarantined body coverage is {body_coverage:.3%}; "
            f"missing {missing_body_ids[:5]}"
        )
    if quarantined_ids:
        warnings.append(f"{len(quarantined_ids)} TDoc artifacts are quarantined")

    return {
        "dataset_version": dataset_version_id,
        "ready": not errors,
        "state": dataset.state,
        "counts": {
            "meetings": meeting_count,
            "undated_meetings": undated_meetings,
            "report_meetings": report_meetings,
            "tdocs": tdoc_count,
            "actionable_tdocs": actionable_tdocs,
            "tdoc_artifacts": len(artifact_ids),
            "parsed_body_documents": parsed_body_documents,
            "quarantined_tdocs": len(quarantined_ids),
            "invalid_artifacts": invalid_artifacts,
        },
        "missing": {
            "archive_tdoc_ids": missing_archive_ids,
            "body_tdoc_ids": missing_body_ids,
        },
        "ratios": {
            "archive_coverage": archive_coverage,
            "body_coverage": body_coverage,
        },
        "errors": errors,
        "warnings": warnings,
    }


async def _count(session: AsyncSession, statement: Any) -> int:
    return int(await session.scalar(statement) or 0)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return min(1.0, numerator / denominator)


def _tdoc_id(filename: str) -> str:
    return Path(filename).stem.upper()


async def activate_dataset(session: AsyncSession, dataset_version_id: str) -> None:
    candidate = await session.scalar(
        select(DatasetVersionRow)
        .where(DatasetVersionRow.id == dataset_version_id)
        .with_for_update()
    )
    if candidate is None:
        raise ValueError(f"dataset version {dataset_version_id} does not exist")
    if candidate.state != DatasetState.VALIDATED:
        raise ValueError("only validated dataset versions can be activated")
    await session.execute(
        update(DatasetVersionRow)
        .where(DatasetVersionRow.is_active.is_(True))
        .values(is_active=False, state=DatasetState.SUPERSEDED)
    )
    candidate.is_active = True
    candidate.state = DatasetState.ACTIVE
    candidate.activated_at = datetime.now(UTC)
    await session.flush()
