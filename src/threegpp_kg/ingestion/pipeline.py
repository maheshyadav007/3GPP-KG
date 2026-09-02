from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import (
    ChunkingConfig,
    EvidenceBlockConfig,
    ParserConfig,
    load_organization_aliases,
)
from ..constants import (
    ArtifactKind,
    DatasetState,
    DocumentState,
    EdgeType,
    EvidenceAuthority,
    SourceRole,
)
from ..domain import DocumentBlock, Meeting, RetrievalChunk, TDoc
from ..graph import GraphFact, validate_graph
from ..parsers.documents import parse_document
from ..parsers.spreadsheet import parse_tdoc_workbook_package
from ..storage.database import (
    ArtifactVersionRow,
    DatasetVersionRow,
    DocumentBlockRow,
    EvidenceRow,
    KnowledgeEdgeRow,
    KnowledgeNodeRow,
    MeetingRow,
    RetrievalChunkRow,
    TDocRow,
)
from ..storage.object_store import ObjectStore
from .blocks import coalesce_evidence_blocks
from .chunking import build_chunks
from .download import DownloadedArtifact
from .normalize import normalize_organization, split_organization_sources


class IngestionValidationError(RuntimeError):
    pass


async def create_dataset(session: AsyncSession, dataset_version_id: str) -> DatasetVersionRow:
    existing = await session.get(DatasetVersionRow, dataset_version_id)
    if existing:
        return existing
    dataset = DatasetVersionRow(
        id=dataset_version_id,
        state=DatasetState.BUILDING,
        created_at=datetime.now(UTC),
        is_active=False,
        stats={},
    )
    session.add(dataset)
    await session.flush()
    return dataset


async def persist_meeting(
    session: AsyncSession, dataset_version_id: str, meeting: Meeting
) -> MeetingRow:
    row = await session.scalar(
        select(MeetingRow).where(
            MeetingRow.dataset_version_id == dataset_version_id,
            MeetingRow.id == meeting.id,
        )
    )
    if row:
        return row
    row = MeetingRow(
        id=meeting.id,
        dataset_version_id=dataset_version_id,
        working_group_id=meeting.working_group_id,
        number=meeting.number,
        variant=meeting.variant,
        name=meeting.name,
        source_url=meeting.source_url,
        starts_on=meeting.starts_on,
        ends_on=meeting.ends_on,
        readiness=meeting.readiness,
    )
    session.add(row)
    await session.flush()
    return row


async def update_meeting_dates(
    session: AsyncSession,
    dataset_version_id: str,
    meeting_id: str,
    starts_on: date,
    ends_on: date,
) -> None:
    """Fill missing meeting dates and propagate temporal validity to its facts."""
    meeting = await session.scalar(
        select(MeetingRow).where(
            MeetingRow.dataset_version_id == dataset_version_id,
            MeetingRow.id == meeting_id,
        )
    )
    if meeting is None:
        raise IngestionValidationError(f"meeting {meeting_id} does not exist")
    if meeting.starts_on not in (None, starts_on) or meeting.ends_on not in (None, ends_on):
        raise IngestionValidationError(f"meeting {meeting_id} has conflicting dates")
    meeting.starts_on = starts_on
    meeting.ends_on = ends_on
    await session.execute(
        update(EvidenceRow)
        .where(
            EvidenceRow.dataset_version_id == dataset_version_id,
            EvidenceRow.meeting_id == meeting_id,
            EvidenceRow.meeting_time.is_(None),
        )
        .values(meeting_time=ends_on)
    )
    meeting_tdocs = select(TDocRow.id).where(
        TDocRow.dataset_version_id == dataset_version_id,
        TDocRow.meeting_id == meeting_id,
    )
    await session.execute(
        update(KnowledgeEdgeRow)
        .where(
            KnowledgeEdgeRow.dataset_version_id == dataset_version_id,
            KnowledgeEdgeRow.valid_at.is_(None),
            or_(
                and_(
                    KnowledgeEdgeRow.source_type == "meeting",
                    KnowledgeEdgeRow.source_id == meeting_id,
                ),
                and_(
                    KnowledgeEdgeRow.source_type == "tdoc",
                    KnowledgeEdgeRow.source_id.in_(meeting_tdocs),
                ),
            ),
        )
        .values(valid_at=ends_on)
    )
    await session.flush()


async def persist_raw_artifact(
    session: AsyncSession,
    object_store: ObjectStore,
    dataset_version_id: str,
    meeting: Meeting,
    filename: str,
    artifact: DownloadedArtifact,
    kind: ArtifactKind,
    *,
    parse_status: str = "not_applicable",
    parse_error: str | None = None,
    ensure_parents: bool = True,
    source_role: SourceRole = SourceRole.OTHER,
    logical_document_id: str | None = None,
    document_id: str | None = None,
    document_state: DocumentState = DocumentState.PUBLISHED,
    published_at: datetime | None = None,
) -> ArtifactVersionRow:
    """Persist an immutable source artifact that does not require content parsing."""
    if ensure_parents:
        await create_dataset(session, dataset_version_id)
        await persist_meeting(session, dataset_version_id, meeting)
    object_key = await object_store.put(
        artifact.sha256, filename, artifact.content, artifact.content_type
    )
    artifact_id = _artifact_id(dataset_version_id, artifact)
    existing = await session.get(ArtifactVersionRow, artifact_id)
    if existing:
        existing.parse_status = parse_status
        existing.parse_error = parse_error
        existing.source_role = source_role
        existing.logical_document_id = logical_document_id
        existing.document_id = document_id
        existing.document_state = document_state
        existing.published_at = published_at
        return existing
    row = ArtifactVersionRow(
        id=artifact_id,
        dataset_version_id=dataset_version_id,
        meeting_id=meeting.id,
        kind=kind,
        source_role=source_role,
        logical_document_id=logical_document_id,
        document_id=document_id,
        document_state=document_state,
        published_at=published_at,
        source_url=artifact.url,
        filename=filename,
        sha256=artifact.sha256,
        content_type=artifact.content_type,
        etag=artifact.etag,
        last_modified=artifact.last_modified,
        object_key=object_key,
        parse_status=parse_status,
        parse_error=parse_error,
        observed_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def ingest_tdoc_workbook(
    session: AsyncSession,
    object_store: ObjectStore,
    dataset_version_id: str,
    meeting: Meeting,
    filename: str,
    artifact: DownloadedArtifact,
    parser_config: ParserConfig | None = None,
) -> list[TDoc]:
    await create_dataset(session, dataset_version_id)
    await persist_meeting(session, dataset_version_id, meeting)
    object_key = await object_store.put(
        artifact.sha256, filename, artifact.content, artifact.content_type
    )
    artifact_id = _artifact_id(dataset_version_id, artifact)
    artifact_row = await session.get(ArtifactVersionRow, artifact_id)
    if artifact_row is None:
        session.add(
            ArtifactVersionRow(
                id=artifact_id,
                dataset_version_id=dataset_version_id,
                meeting_id=meeting.id,
                kind=ArtifactKind.TDOC_LIST,
                source_role=SourceRole.TDOC_LIST,
                logical_document_id=f"tdoc-list:{meeting.id}",
                source_url=artifact.url,
                filename=filename,
                sha256=artifact.sha256,
                content_type=artifact.content_type,
                etag=artifact.etag,
                last_modified=artifact.last_modified,
                object_key=object_key,
                parse_status="parsed",
                observed_at=datetime.now(UTC),
            )
        )
    else:
        artifact_row.parse_status = "parsed"
        artifact_row.parse_error = None
    sheets = parse_tdoc_workbook_package(
        artifact.content,
        filename,
        meeting.id,
        artifact.url,
        parser_config,
    )
    tdocs_by_id = {tdoc.id: tdoc for sheet in sheets for tdoc in sheet.tdocs}
    tdocs = list(tdocs_by_id.values())
    evidence_ids: list[str] = []
    for tdoc in tdocs:
        evidence_id = _row_evidence_id(dataset_version_id, artifact.sha256, tdoc.id)
        tdoc.evidence_ids = [evidence_id]
        evidence_ids.append(evidence_id)

    existing_evidence = await _existing_evidence_ids(session, evidence_ids)
    session.add_all(
        [
            EvidenceRow(
                id=tdoc.evidence_ids[0],
                dataset_version_id=dataset_version_id,
                source_url=artifact.url,
                artifact_sha256=artifact.sha256,
                authority=EvidenceAuthority.MEETING_EXPORT,
                meeting_id=meeting.id,
                tdoc_id=tdoc.id,
                section_path=[tdoc.agenda_item] if tdoc.agenda_item else [],
                block_id=f"sheet-row:{tdoc.id}",
                excerpt=_evidence_excerpt(tdoc),
                extraction_method=(
                    "xlsx_row_from_zip" if filename.casefold().endswith(".zip") else "xlsx_row"
                ),
                extractor_version="openpyxl-3",
                confidence=1.0,
                meeting_time=meeting.ends_on,
                observed_at=datetime.now(UTC),
            )
            for tdoc in tdocs
            if tdoc.evidence_ids[0] not in existing_evidence
        ]
    )

    existing_tdocs = await _existing_tdoc_meetings(
        session, dataset_version_id, [tdoc.id for tdoc in tdocs]
    )
    session.add_all(
        [_tdoc_row(dataset_version_id, tdoc) for tdoc in tdocs if tdoc.id not in existing_tdocs]
    )
    graph_tdocs = [
        tdoc
        for tdoc in tdocs
        if existing_tdocs.get(tdoc.id) in {None, meeting.id}
    ]
    await persist_tdoc_graph_batch(session, dataset_version_id, meeting, graph_tdocs)
    await session.flush()
    return tdocs


async def _existing_evidence_ids(session: AsyncSession, identifiers: list[str]) -> set[str]:
    existing: set[str] = set()
    for batch in _batches(identifiers):
        existing.update(
            await session.scalars(select(EvidenceRow.id).where(EvidenceRow.id.in_(batch)))
        )
    return existing


async def _existing_tdoc_meetings(
    session: AsyncSession, dataset_version_id: str, identifiers: list[str]
) -> dict[str, str]:
    existing: dict[str, str] = {}
    for batch in _batches(identifiers):
        rows = (
            await session.execute(
                select(TDocRow.id, TDocRow.meeting_id).where(
                    TDocRow.dataset_version_id == dataset_version_id,
                    TDocRow.id.in_(batch),
                )
            )
        ).all()
        existing.update({identifier: meeting_id for identifier, meeting_id in rows})
    return existing


def _batches(values: list[str], size: int = 1000) -> list[list[str]]:
    return [values[offset : offset + size] for offset in range(0, len(values), size)]


async def ingest_document_artifact(
    session: AsyncSession,
    object_store: ObjectStore,
    dataset_version_id: str,
    meeting: Meeting,
    document_id: str,
    filename: str,
    artifact: DownloadedArtifact,
    chunking: ChunkingConfig,
    *,
    evidence_blocks: EvidenceBlockConfig | None = None,
    parser_config: ParserConfig | None = None,
    kind: ArtifactKind = ArtifactKind.TDOC,
    authority: EvidenceAuthority = EvidenceAuthority.TDOC_BODY,
    ensure_parents: bool = True,
    assume_new: bool = False,
    source_role: SourceRole = SourceRole.TDOC,
    logical_document_id: str | None = None,
    document_state: DocumentState = DocumentState.PUBLISHED,
    published_at: datetime | None = None,
) -> tuple[list[DocumentBlock], list[RetrievalChunk]]:
    if ensure_parents:
        await create_dataset(session, dataset_version_id)
        await persist_meeting(session, dataset_version_id, meeting)
    object_key = await object_store.put(
        artifact.sha256, filename, artifact.content, artifact.content_type
    )
    artifact_id = _artifact_id(dataset_version_id, artifact)
    artifact_row = await session.get(ArtifactVersionRow, artifact_id)
    if artifact_row is None:
        session.add(
            ArtifactVersionRow(
                id=artifact_id,
                dataset_version_id=dataset_version_id,
                meeting_id=meeting.id,
                kind=kind,
                source_role=source_role,
                logical_document_id=logical_document_id or document_id,
                document_id=document_id,
                document_state=document_state,
                published_at=published_at,
                source_url=artifact.url,
                filename=filename,
                sha256=artifact.sha256,
                content_type=artifact.content_type,
                etag=artifact.etag,
                last_modified=artifact.last_modified,
                object_key=object_key,
                parse_status="parsed",
                observed_at=datetime.now(UTC),
            )
        )
    else:
        artifact_row.parse_status = "parsed"
        artifact_row.parse_error = None
        artifact_row.source_role = source_role
        artifact_row.logical_document_id = logical_document_id or document_id
        artifact_row.document_id = document_id
        artifact_row.document_state = document_state
        artifact_row.published_at = published_at
    source_blocks = await asyncio.to_thread(
        parse_document, artifact.content, filename, document_id, parser_config
    )
    if parser_config and len(source_blocks) > parser_config.max_document_blocks:
        raise IngestionValidationError(
            f"document has {len(source_blocks)} source elements; "
            f"limit is {parser_config.max_document_blocks}"
        )
    blocks = coalesce_evidence_blocks(source_blocks, evidence_blocks or chunking)
    existing_block_ids: set[str] = set()
    existing_evidence_ids: set[str] = set()
    if not assume_new:
        existing_block_ids = set(
            await session.scalars(
                select(DocumentBlockRow.id).where(
                    DocumentBlockRow.dataset_version_id == dataset_version_id,
                    DocumentBlockRow.document_id == document_id,
                )
            )
        )
        existing_evidence_ids = set(
            await session.scalars(
                select(EvidenceRow.id).where(
                    EvidenceRow.dataset_version_id == dataset_version_id,
                    EvidenceRow.artifact_sha256 == artifact.sha256,
                )
            )
        )
    evidence_by_block: dict[str, str] = {}
    for block in blocks:
        evidence_id = _block_evidence_id(dataset_version_id, artifact.sha256, block.id)
        evidence_by_block[block.id] = evidence_id
        if block.id not in existing_block_ids:
            session.add(
                DocumentBlockRow(
                    id=block.id,
                    dataset_version_id=dataset_version_id,
                    document_id=block.document_id,
                    block_index=block.index,
                    kind=block.kind,
                    text=block.text,
                    section_path=block.section_path,
                    table_row=block.table_row,
                )
            )
        if evidence_id not in existing_evidence_ids:
            session.add(
                EvidenceRow(
                    id=evidence_id,
                    dataset_version_id=dataset_version_id,
                    source_url=artifact.url,
                    artifact_sha256=artifact.sha256,
                    authority=authority,
                    meeting_id=meeting.id,
                    tdoc_id=document_id if kind == ArtifactKind.TDOC else None,
                    section_path=block.section_path,
                    block_id=block.id,
                    excerpt=block.text[:500],
                    extraction_method=f"{filename.rsplit('.', 1)[-1].lower()}_section_block",
                    extractor_version="threegpp-evidence-graph-section-blocks-v2",
                    confidence=1.0,
                    meeting_time=meeting.ends_on,
                    observed_at=datetime.now(UTC),
                )
            )
    chunks = build_chunks(blocks, chunking)
    existing_chunk_ids: set[str] = set()
    if not assume_new:
        existing_chunk_ids = set(
            await session.scalars(
                select(RetrievalChunkRow.id).where(
                    RetrievalChunkRow.dataset_version_id == dataset_version_id,
                    RetrievalChunkRow.document_id == document_id,
                )
            )
        )
    persisted_chunks: list[RetrievalChunk] = []
    for chunk in chunks:
        evidence_ids = list(
            dict.fromkeys(evidence_by_block[block_id] for block_id in chunk.block_ids)
        )
        chunk = chunk.model_copy(update={"evidence_ids": evidence_ids})
        persisted_chunks.append(chunk)
        if chunk.id not in existing_chunk_ids:
            session.add(
                RetrievalChunkRow(
                    id=chunk.id,
                    dataset_version_id=dataset_version_id,
                    document_id=chunk.document_id,
                    block_ids=chunk.block_ids,
                    text=chunk.text,
                    section_path=chunk.section_path,
                    token_count=chunk.token_count,
                    evidence_ids=chunk.evidence_ids,
                )
            )
    await session.flush()
    return blocks, persisted_chunks


async def validate_dataset(session: AsyncSession, dataset_version_id: str) -> dict[str, int]:
    dataset = await session.get(DatasetVersionRow, dataset_version_id)
    if dataset is None:
        raise IngestionValidationError("dataset does not exist")
    tdoc_count = await session.scalar(
        select(func.count(TDocRow.row_id)).where(TDocRow.dataset_version_id == dataset_version_id)
    )
    meeting_count = await session.scalar(
        select(func.count(MeetingRow.row_id)).where(
            MeetingRow.dataset_version_id == dataset_version_id
        )
    )
    if not tdoc_count or not meeting_count:
        raise IngestionValidationError("dataset must contain at least one meeting and one TDoc")
    node_rows = list(
        (
            await session.scalars(
                select(KnowledgeNodeRow).where(
                    KnowledgeNodeRow.dataset_version_id == dataset_version_id
                )
            )
        ).all()
    )
    edge_rows = list(
        (
            await session.scalars(
                select(KnowledgeEdgeRow).where(
                    KnowledgeEdgeRow.dataset_version_id == dataset_version_id
                )
            )
        ).all()
    )
    graph_errors = validate_graph(
        {(row.entity_type, row.id) for row in node_rows},
        [
            GraphFact(
                row.source_type,
                row.source_id,
                row.predicate,
                row.target_type,
                row.target_id,
            )
            for row in edge_rows
        ],
    )
    if graph_errors:
        raise IngestionValidationError("; ".join(graph_errors[:10]))
    stats = {
        "meetings": int(meeting_count),
        "tdocs": int(tdoc_count),
        "nodes": len(node_rows),
        "edges": len(edge_rows),
    }
    dataset.stats = stats
    dataset.state = DatasetState.VALIDATED
    await session.flush()
    return stats


def _row_evidence_id(dataset_version_id: str, sha256: str, tdoc_id: str) -> str:
    digest = hashlib.sha256(f"{dataset_version_id}|{sha256}|{tdoc_id}".encode()).hexdigest()
    return "evidence-" + digest[:24]


def _block_evidence_id(dataset_version_id: str, sha256: str, block_id: str) -> str:
    digest = hashlib.sha256(f"{dataset_version_id}|{sha256}|{block_id}".encode()).hexdigest()
    return "evidence-" + digest[:24]


def _artifact_id(dataset_version_id: str, artifact: DownloadedArtifact) -> str:
    digest = hashlib.sha256(
        f"{dataset_version_id}|{artifact.url}|{artifact.sha256}".encode()
    ).hexdigest()
    return "artifact-" + digest[:32]


def artifact_version_id(dataset_version_id: str, artifact: DownloadedArtifact) -> str:
    return _artifact_id(dataset_version_id, artifact)


def _evidence_excerpt(tdoc: TDoc) -> str:
    pieces = [tdoc.id, tdoc.title, tdoc.status_raw]
    return " | ".join(piece for piece in pieces if piece)[:500]


def _tdoc_row(dataset_version_id: str, tdoc: TDoc) -> TDocRow:
    return TDocRow(
        id=tdoc.id,
        dataset_version_id=dataset_version_id,
        meeting_id=tdoc.meeting_id,
        title=tdoc.title,
        source=tdoc.source,
        document_type=tdoc.document_type,
        purpose=tdoc.purpose,
        agenda_item=tdoc.agenda_item,
        agenda_description=tdoc.agenda_description,
        status=tdoc.status,
        status_raw=tdoc.status_raw,
        abstract=tdoc.abstract,
        summary=tdoc.summary,
        discussion=tdoc.discussion,
        conclusion_text=tdoc.conclusion_text,
        revised_from=tdoc.revised_from,
        revised_to=tdoc.revised_to,
        releases=tdoc.releases,
        specifications=tdoc.specifications,
        work_items=tdoc.work_items,
        cr_number=tdoc.cr_number,
        cr_revision=tdoc.cr_revision,
        cr_category=tdoc.cr_category,
        source_url=tdoc.source_url,
        evidence_ids=tdoc.evidence_ids,
    )


async def persist_tdoc_graph_batch(
    session: AsyncSession,
    dataset_version_id: str,
    meeting: Meeting,
    tdocs: list[TDoc],
) -> None:
    aliases = load_organization_aliases()
    node_facts: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}
    edge_facts: dict[str, tuple[str, str, EdgeType, str, str, list[str]]] = {}
    for tdoc in tdocs:
        nodes, edges = _tdoc_graph_facts(meeting, tdoc, aliases)
        for entity_type, identifier, label, properties in nodes:
            key = (entity_type, identifier)
            existing_fact = node_facts.get(key)
            if existing_fact is None or (
                existing_fact[1].get("placeholder") == "true"
                and properties.get("placeholder") != "true"
            ):
                node_facts[key] = (label, properties)
        for source_type, source_id, predicate, target_type, target_id in edges:
            edge_id = _edge_id(
                dataset_version_id, source_type, source_id, predicate, target_type, target_id
            )
            edge_facts[edge_id] = (
                source_type,
                source_id,
                predicate,
                target_type,
                target_id,
                tdoc.evidence_ids,
            )

    existing_nodes: dict[tuple[str, str], KnowledgeNodeRow] = {}
    node_ids = list({identifier for _, identifier in node_facts})
    for batch in _batches(node_ids):
        rows = await session.scalars(
            select(KnowledgeNodeRow).where(
                KnowledgeNodeRow.dataset_version_id == dataset_version_id,
                KnowledgeNodeRow.id.in_(batch),
            )
        )
        existing_nodes.update({(row.entity_type, row.id): row for row in rows})

    new_nodes: list[KnowledgeNodeRow] = []
    for (entity_type, identifier), (label, properties) in node_facts.items():
        existing_node = existing_nodes.get((entity_type, identifier))
        if existing_node is None:
            new_nodes.append(
                KnowledgeNodeRow(
                    dataset_version_id=dataset_version_id,
                    entity_type=entity_type,
                    id=identifier,
                    label=label,
                    properties=properties,
                )
            )
        elif (
            existing_node.properties.get("placeholder") == "true"
            and properties.get("placeholder") != "true"
        ):
            existing_node.label = label
            existing_node.properties = properties
    session.add_all(new_nodes)

    existing_edges: set[str] = set()
    for batch in _batches(list(edge_facts)):
        existing_edges.update(
            await session.scalars(select(KnowledgeEdgeRow.id).where(KnowledgeEdgeRow.id.in_(batch)))
        )
    session.add_all(
        [
            KnowledgeEdgeRow(
                id=edge_id,
                dataset_version_id=dataset_version_id,
                source_type=fact[0],
                source_id=fact[1],
                predicate=fact[2],
                target_type=fact[3],
                target_id=fact[4],
                evidence_ids=fact[5],
                confidence=1.0,
                valid_at=meeting.ends_on,
            )
            for edge_id, fact in edge_facts.items()
            if edge_id not in existing_edges
        ]
    )


def _tdoc_graph_facts(
    meeting: Meeting,
    tdoc: TDoc,
    organization_aliases: dict[str, str],
) -> tuple[
    list[tuple[str, str, str, dict[str, str]]],
    list[tuple[str, str, EdgeType, str, str]],
]:
    nodes: list[tuple[str, str, str, dict[str, str]]] = [
        ("meeting", meeting.id, meeting.name, {"working_group": meeting.working_group_id}),
        ("tdoc", tdoc.id, tdoc.title or tdoc.id, {"status": tdoc.status.value}),
    ]
    edges: list[tuple[str, str, EdgeType, str, str]] = [
        ("meeting", meeting.id, EdgeType.CONTAINS, "tdoc", tdoc.id)
    ]
    for company in split_organization_sources(tdoc.source):
        company = normalize_organization(company, organization_aliases)
        company_id = _canonical_identifier(company)
        nodes.append(("organization", company_id, company, {}))
        edges.append(("tdoc", tdoc.id, EdgeType.SUBMITTED_BY, "organization", company_id))
    if tdoc.agenda_item or tdoc.agenda_description:
        agenda_id = (
            f"{meeting.id}:{_canonical_identifier(tdoc.agenda_item or tdoc.agenda_description)}"
        )
        nodes.append(
            (
                "agenda_item",
                agenda_id,
                tdoc.agenda_description or tdoc.agenda_item,
                {"number": tdoc.agenda_item},
            )
        )
        edges.append(("tdoc", tdoc.id, EdgeType.BELONGS_TO_AGENDA, "agenda_item", agenda_id))
    if tdoc.agenda_description:
        topic_id = _canonical_identifier(tdoc.agenda_description)
        nodes.append(("topic", topic_id, tdoc.agenda_description, {}))
        edges.append(("tdoc", tdoc.id, EdgeType.MENTIONS_TOPIC, "topic", topic_id))
    for specification in tdoc.specifications:
        specification_id = _canonical_identifier(specification)
        nodes.append(("specification", specification_id, specification, {}))
        edges.append(("tdoc", tdoc.id, EdgeType.AFFECTS_SPEC, "specification", specification_id))
    for release in tdoc.releases:
        release_id = _canonical_identifier(release)
        nodes.append(("release", release_id, release, {}))
        edges.append(("tdoc", tdoc.id, EdgeType.TARGETS_RELEASE, "release", release_id))
    for work_item in tdoc.work_items:
        work_item_id = _canonical_identifier(work_item)
        nodes.append(("work_item", work_item_id, work_item, {}))
        edges.append(("tdoc", tdoc.id, EdgeType.RELATED_TO_WORK_ITEM, "work_item", work_item_id))
    if tdoc.cr_number:
        cr_id = f"{tdoc.meeting_id}:{_canonical_identifier(tdoc.cr_number)}"
        nodes.append(
            (
                "change_request",
                cr_id,
                f"CR {tdoc.cr_number}",
                {"revision": tdoc.cr_revision or "", "category": tdoc.cr_category or ""},
            )
        )
        edges.append(("tdoc", tdoc.id, EdgeType.HAS_CHANGE_REQUEST, "change_request", cr_id))
    if tdoc.revised_from:
        nodes.append(("tdoc", tdoc.revised_from, tdoc.revised_from, {"placeholder": "true"}))
        edges.append(("tdoc", tdoc.id, EdgeType.REVISES, "tdoc", tdoc.revised_from))
    return nodes, edges


def _canonical_identifier(value: str) -> str:
    identifier = "-".join(value.lower().replace("/", " ").split())
    if len(identifier) <= 80:
        return identifier
    digest = hashlib.sha256(identifier.encode()).hexdigest()[:16]
    return f"{identifier[:63]}-{digest}"


def _edge_id(
    dataset_version_id: str,
    source_type: str,
    source_id: str,
    predicate: EdgeType,
    target_type: str,
    target_id: str,
) -> str:
    value = "|".join(
        (dataset_version_id, source_type, source_id, predicate.value, target_type, target_id)
    )
    return "edge-" + hashlib.sha256(value.encode()).hexdigest()[:32]
