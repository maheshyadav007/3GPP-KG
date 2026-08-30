from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import aliased

from threegpp_kg.config import load_settings
from threegpp_kg.constants import ArtifactKind
from threegpp_kg.storage.database import (
    ArtifactVersionRow,
    DocumentBlockRow,
    KnowledgeEdgeRow,
    KnowledgeNodeRow,
    MeetingRow,
    RetrievalChunkRow,
    TDocRow,
    create_engine_and_session,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile local manifests with one dataset")
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--manifest", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def load_expected(paths: list[Path]) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    meetings: list[str] = []
    for path in paths:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        selected = manifest.get("selected_meetings") or list(manifest["meetings"])
        meetings.extend(selected)
        for entry in manifest["artifacts"].values():
            if entry["meeting_id"] not in selected:
                continue
            expected[(entry["url"], entry["sha256"])] = entry
    return expected, meetings


async def reconcile(
    dataset_version: str,
    expected: dict[tuple[str, str], dict[str, Any]],
    meeting_ids: list[str],
) -> dict[str, Any]:
    engine, sessions = create_engine_and_session(load_settings().database)
    try:
        async with sessions() as session:
            artifacts = list(
                await session.scalars(
                    select(ArtifactVersionRow).where(
                        ArtifactVersionRow.dataset_version_id == dataset_version,
                        ArtifactVersionRow.meeting_id.in_(meeting_ids),
                    )
                )
            )
            actual = {(row.source_url, row.sha256): row for row in artifacts}
            block_document_ids = set(
                await session.scalars(
                    select(DocumentBlockRow.document_id)
                    .where(DocumentBlockRow.dataset_version_id == dataset_version)
                    .distinct()
                )
            )
            parsed_tdoc_ids = {
                Path(row.filename).stem.upper()
                for row in artifacts
                if row.kind == ArtifactKind.TDOC and row.parse_status == "parsed"
            }
            authoritative_tdocs = set(
                await session.execute(
                    select(TDocRow.meeting_id, TDocRow.id).where(
                        TDocRow.dataset_version_id == dataset_version
                    )
                )
            )
            missing_block_ids = sorted(parsed_tdoc_ids - block_document_ids)
            source_node = aliased(KnowledgeNodeRow)
            target_node = aliased(KnowledgeNodeRow)
            orphan_edges = int(
                await session.scalar(
                    select(func.count(KnowledgeEdgeRow.id))
                    .outerjoin(
                        source_node,
                        and_(
                            source_node.dataset_version_id
                            == KnowledgeEdgeRow.dataset_version_id,
                            source_node.entity_type == KnowledgeEdgeRow.source_type,
                            source_node.id == KnowledgeEdgeRow.source_id,
                        ),
                    )
                    .outerjoin(
                        target_node,
                        and_(
                            target_node.dataset_version_id
                            == KnowledgeEdgeRow.dataset_version_id,
                            target_node.entity_type == KnowledgeEdgeRow.target_type,
                            target_node.id == KnowledgeEdgeRow.target_id,
                        ),
                    )
                    .where(
                        KnowledgeEdgeRow.dataset_version_id == dataset_version,
                        (source_node.id.is_(None) | target_node.id.is_(None)),
                    )
                )
                or 0
            )
            counts = {
                "meetings": await _count(
                    session,
                    MeetingRow.row_id,
                    MeetingRow.dataset_version_id == dataset_version,
                ),
                "tdocs": await _count(
                    session, TDocRow.row_id, TDocRow.dataset_version_id == dataset_version
                ),
                "blocks": await _count(
                    session,
                    DocumentBlockRow.id,
                    DocumentBlockRow.dataset_version_id == dataset_version,
                ),
                "chunks": await _count(
                    session,
                    RetrievalChunkRow.id,
                    RetrievalChunkRow.dataset_version_id == dataset_version,
                ),
                "nodes": await _count(
                    session,
                    KnowledgeNodeRow.id,
                    KnowledgeNodeRow.dataset_version_id == dataset_version,
                ),
                "edges": await _count(
                    session,
                    KnowledgeEdgeRow.id,
                    KnowledgeEdgeRow.dataset_version_id == dataset_version,
                ),
            }
            undated = list(
                await session.scalars(
                    select(MeetingRow.id).where(
                        MeetingRow.dataset_version_id == dataset_version,
                        (MeetingRow.starts_on.is_(None) | MeetingRow.ends_on.is_(None)),
                    )
                )
            )

        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        by_kind_status = Counter((str(row.kind), row.parse_status) for row in artifacts)
        by_meeting: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
        for row in artifacts:
            by_meeting[row.meeting_id][(str(row.kind), row.parse_status)] += 1
        failures = [_artifact_detail(row) for row in artifacts if row.parse_status == "failed"]
        quarantined = [
            _artifact_detail(row) for row in artifacts if row.parse_status == "quarantined"
        ]
        report_meetings = {
            row.meeting_id
            for row in artifacts
            if row.kind == ArtifactKind.REPORT and row.parse_status == "parsed"
        }
        tdoc_total = sum(row.kind == ArtifactKind.TDOC for row in artifacts)
        tdoc_parsed = sum(
            row.kind == ArtifactKind.TDOC and row.parse_status == "parsed" for row in artifacts
        )
        accounted = len(expected) - len(missing)
        authoritative_bodies = [
            row
            for row in artifacts
            if row.kind == ArtifactKind.TDOC
            and (row.meeting_id, Path(row.filename).stem.upper()) in authoritative_tdocs
            and not (row.parse_error or "").startswith("redundant source copy")
        ]
        authoritative_statuses = Counter(row.parse_status for row in authoritative_bodies)
        issues: list[str] = []
        if missing:
            issues.append(f"{len(missing)} manifest artifacts have no matching database row")
        if failures:
            issues.append(f"{len(failures)} artifacts failed deterministic extraction")
        if missing_block_ids:
            issues.append(f"{len(missing_block_ids)} parsed TDocs have no evidence blocks")
        if orphan_edges:
            issues.append(f"{orphan_edges} graph edges have a missing endpoint")
        if undated:
            issues.append(f"{len(undated)} meetings have incomplete normalized dates")
        meetings_without_reports = sorted(set(meeting_ids) - report_meetings)
        if meetings_without_reports:
            issues.append(f"{len(meetings_without_reports)} meetings have no parsed report")

        return {
            "dataset_version": dataset_version,
            "ready_for_activation": not issues,
            "counts": counts,
            "manifest": {
                "expected_artifacts": len(expected),
                "accounted_artifacts": accounted,
                "coverage": accounted / len(expected) if expected else 0.0,
                "missing": [
                    {"url": url, "sha256": sha256} for url, sha256 in missing
                ],
                "unexpected_database_rows": [
                    {"url": url, "sha256": sha256} for url, sha256 in unexpected
                ],
            },
            "tdoc_bodies": {
                "artifacts": tdoc_total,
                "parsed": tdoc_parsed,
                "parsed_ratio": tdoc_parsed / tdoc_total if tdoc_total else 0.0,
                "parsed_without_blocks": missing_block_ids,
            },
            "authoritative_tdoc_bodies": {
                "artifacts": len(authoritative_bodies),
                "statuses": dict(sorted(authoritative_statuses.items())),
                "parsed_ratio": (
                    authoritative_statuses["parsed"] / len(authoritative_bodies)
                    if authoritative_bodies
                    else 0.0
                ),
                "parsed_or_quarantined_ratio": (
                    (
                        authoritative_statuses["parsed"]
                        + authoritative_statuses["quarantined"]
                    )
                    / len(authoritative_bodies)
                    if authoritative_bodies
                    else 0.0
                ),
            },
            "by_kind_status": _counter_rows(by_kind_status),
            "by_meeting": {
                meeting_id: _counter_rows(by_meeting[meeting_id])
                for meeting_id in sorted(meeting_ids)
            },
            "graph": {"orphan_edges": orphan_edges},
            "undated_meetings": sorted(undated),
            "meetings_without_parsed_reports": meetings_without_reports,
            "failures": failures,
            "quarantined": quarantined,
            "issues": issues,
        }
    finally:
        await engine.dispose()


async def _count(session: Any, column: Any, condition: Any) -> int:
    return int(await session.scalar(select(func.count(column)).where(condition)) or 0)


def _counter_rows(counter: Counter[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        {"kind": kind, "status": status, "count": count}
        for (kind, status), count in sorted(counter.items())
    ]


def _artifact_detail(row: ArtifactVersionRow) -> dict[str, str]:
    return {
        "meeting_id": row.meeting_id,
        "kind": str(row.kind),
        "filename": row.filename,
        "sha256": row.sha256,
        "source_url": row.source_url,
        "error": row.parse_error or "",
    }


def main() -> None:
    args = parse_args()
    expected, meeting_ids = load_expected(args.manifest)
    result = asyncio.run(reconcile(args.dataset_version, expected, meeting_ids))
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
