from __future__ import annotations

import hashlib
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import ChunkingConfig, EvidenceBlockConfig, ParserConfig
from .constants import (
    STATUS_ALIASES,
    ArtifactKind,
    BlockKind,
    Conclusion,
    DocumentState,
    EdgeType,
    EvidenceAuthority,
    ObservationType,
    SourceRole,
)
from .domain import DocumentBlock, Meeting, MeetingObservation, RetrievalChunk
from .ingestion.download import DownloadedArtifact
from .ingestion.pipeline import artifact_version_id, ingest_document_artifact
from .storage.database import (
    EvidenceRow,
    KnowledgeEdgeRow,
    KnowledgeNodeRow,
    MeetingObservationRow,
)
from .storage.object_store import ObjectStore

_TDOC_RE = re.compile(r"\b([A-Z]\d)[-_](\d{6,7})\b", re.IGNORECASE)
_SPEC_RE = re.compile(r"\b(?:2[134568]|3[678])\.\d{3}\b")
_AGENDA_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:\s|$)")
_VERSION_SUFFIX_RE = re.compile(
    r"(?:[_ -](?:v|rev|draft|final|clean|eom)\d*)+$", re.IGNORECASE
)
_DECISION_PREFIXES = (
    "agreement:",
    "conclusion:",
    "agreed",
    "approved",
    "endorsed",
    "rejected",
    "withdrawn",
    "not pursued",
    "not treated",
    "postponed",
)
_OPEN_ISSUE_MARKERS = ("ffs", "open issue", "remains open", "revisit", "to be discussed")
_ACTION_MARKERS = (
    "action:",
    "next meeting",
    "offline discussion",
    "send an ls",
    "send a liaison",
    "provide a revision",
    "provide a revised",
)
_DEPENDENCY_MARKERS = ("wait for", "depending on", "depends on", "subject to", "after ran")


async def ingest_meeting_source(
    session: AsyncSession,
    object_store: ObjectStore,
    dataset_version_id: str,
    meeting: Meeting,
    filename: str,
    artifact: DownloadedArtifact,
    source_role: SourceRole,
    chunking: ChunkingConfig,
    *,
    evidence_blocks: EvidenceBlockConfig | None = None,
    parser_config: ParserConfig | None = None,
) -> tuple[list[DocumentBlock], list[RetrievalChunk], list[MeetingObservation]]:
    logical_document_id = source_logical_document_id(meeting.id, source_role, filename)
    document_id = f"{logical_document_id}:{artifact.sha256[:12]}"
    authority = source_authority(source_role, filename)
    state = source_document_state(source_role, filename)
    published_at = _parse_http_datetime(artifact.last_modified)
    blocks, chunks = await ingest_document_artifact(
        session,
        object_store,
        dataset_version_id,
        meeting,
        document_id,
        filename,
        artifact,
        chunking,
        evidence_blocks=evidence_blocks,
        parser_config=parser_config,
        kind=source_artifact_kind(source_role),
        authority=authority,
        source_role=source_role,
        logical_document_id=logical_document_id,
        document_state=state,
        published_at=published_at,
    )
    artifact_id = artifact_version_id(dataset_version_id, artifact)
    evidence_rows = (
        await session.scalars(
            select(EvidenceRow).where(
                EvidenceRow.dataset_version_id == dataset_version_id,
                EvidenceRow.artifact_sha256 == artifact.sha256,
            )
        )
    ).all()
    evidence_by_block = {
        row.block_id: row.id for row in evidence_rows if row.block_id is not None
    }
    observations = extract_meeting_observations(
        blocks,
        meeting_id=meeting.id,
        artifact_version_id=artifact_id,
        source_role=source_role,
        authority=authority,
        evidence_by_block=evidence_by_block,
        effective_at=published_at,
    )
    await persist_meeting_observations(session, dataset_version_id, meeting, observations)
    return blocks, chunks, observations


def extract_meeting_observations(
    blocks: list[DocumentBlock],
    *,
    meeting_id: str,
    artifact_version_id: str,
    source_role: SourceRole,
    authority: EvidenceAuthority,
    evidence_by_block: dict[str, str],
    effective_at: datetime | None = None,
) -> list[MeetingObservation]:
    observations: list[MeetingObservation] = []
    agenda_item = ""
    contextual_tdocs: list[str] = []
    for block in blocks:
        if block.kind == BlockKind.HEADING:
            match = _AGENDA_RE.match(block.text.strip())
            if match:
                agenda_item = match.group(1)
            heading_tdocs = _tdoc_ids(block.text)
            if heading_tdocs:
                contextual_tdocs = heading_tdocs
            continue
        for section in reversed(block.section_path):
            match = _AGENDA_RE.match(section.strip())
            if match:
                agenda_item = match.group(1)
                break
        section_tdocs = _tdoc_ids(" ".join(block.section_path))
        for text in _observation_texts(block.text, source_role):
            if len(text) < 8:
                continue
            own_tdocs = _tdoc_ids(text)
            tdoc_ids = own_tdocs or section_tdocs or contextual_tdocs
            lowered = text.casefold()
            observation_types = _observation_types(block.kind, lowered, source_role)
            for observation_type in observation_types:
                conclusion = (
                    _conclusion(text)
                    if observation_type == ObservationType.DECISION
                    else None
                )
                content_hash = _digest(_normalize_observation_text(text))
                observation_key = _observation_key(
                    observation_type, agenda_item, tdoc_ids, text
                )
                evidence_id = evidence_by_block.get(block.id)
                identifier = _digest(
                    "|".join(
                        (
                            artifact_version_id,
                            observation_type,
                            content_hash,
                            observation_key,
                        )
                    )
                )
                observations.append(
                    MeetingObservation(
                        id=f"observation-{identifier[:28]}",
                        meeting_id=meeting_id,
                        artifact_version_id=artifact_version_id,
                        source_role=source_role,
                        authority=authority,
                        observation_type=observation_type,
                        observation_key=observation_key,
                        text=text,
                        agenda_item=agenda_item,
                        tdoc_ids=tdoc_ids,
                        specification_ids=sorted(
                            set(_SPEC_RE.findall(" ".join([*block.section_path, text])))
                        ),
                        conclusion=conclusion,
                        evidence_ids=[evidence_id] if evidence_id else [],
                        content_hash=content_hash,
                        effective_at=effective_at,
                    )
                )
    return list({item.id: item for item in observations}.values())


async def persist_meeting_observations(
    session: AsyncSession,
    dataset_version_id: str,
    meeting: Meeting,
    observations: list[MeetingObservation],
) -> None:
    if not observations:
        return
    observations = list({item.id: item for item in observations}.values())
    existing = set(
        await session.scalars(
            select(MeetingObservationRow.id).where(
                MeetingObservationRow.id.in_([item.id for item in observations])
            )
        )
    )
    session.add_all(
        [
            MeetingObservationRow(
                id=item.id,
                dataset_version_id=dataset_version_id,
                meeting_id=item.meeting_id,
                artifact_version_id=item.artifact_version_id,
                source_role=item.source_role,
                authority=item.authority,
                observation_type=item.observation_type,
                observation_key=item.observation_key,
                text=item.text,
                agenda_item=item.agenda_item,
                tdoc_ids=item.tdoc_ids,
                specification_ids=item.specification_ids,
                work_item_ids=item.work_item_ids,
                conclusion=item.conclusion,
                evidence_ids=item.evidence_ids,
                content_hash=item.content_hash,
                effective_at=item.effective_at,
                confidence=item.confidence,
            )
            for item in observations
            if item.id not in existing
        ]
    )
    await _persist_observation_graph(session, dataset_version_id, meeting, observations)
    await session.flush()


def source_logical_document_id(
    meeting_id: str, source_role: SourceRole, filename: str
) -> str:
    if source_role in {SourceRole.CHAIR_NOTES, SourceRole.REPORT}:
        return f"{source_role}:{meeting_id}"
    stem = PurePosixPath(filename).stem
    stem = _VERSION_SUFFIX_RE.sub("", stem)
    stem = re.sub(r"[^a-z0-9]+", "-", stem.casefold()).strip("-")
    return f"{source_role}:{meeting_id}:{stem or 'document'}"


def source_artifact_kind(source_role: SourceRole) -> ArtifactKind:
    return {
        SourceRole.REPORT: ArtifactKind.REPORT,
        SourceRole.CHAIR_NOTES: ArtifactKind.CHAIR_NOTES,
        SourceRole.POST_MEETING_DISCUSSION: ArtifactKind.POST_MEETING_DISCUSSION,
    }.get(source_role, ArtifactKind.OTHER)


def source_authority(source_role: SourceRole, filename: str) -> EvidenceAuthority:
    if source_role == SourceRole.CHAIR_NOTES:
        return EvidenceAuthority.CHAIR_NOTES
    if source_role == SourceRole.POST_MEETING_DISCUSSION:
        return EvidenceAuthority.POST_MEETING_DISCUSSION
    lowered = filename.casefold()
    if "approved" in lowered:
        return EvidenceAuthority.APPROVED_REPORT
    if "draft" in lowered or "skeleton" in lowered:
        return EvidenceAuthority.DRAFT_REPORT
    return EvidenceAuthority.FINAL_REPORT


def source_document_state(source_role: SourceRole, filename: str) -> DocumentState:
    lowered = filename.casefold()
    if "approved" in lowered:
        return DocumentState.APPROVED
    if "draft" in lowered or "skeleton" in lowered:
        return DocumentState.WORKING
    if "final" in lowered or "clean" in lowered or "eom" in lowered:
        return DocumentState.FINAL_CLEAN
    if source_role == SourceRole.REPORT:
        return DocumentState.SUBMITTED_FOR_APPROVAL
    return DocumentState.PUBLISHED


def _observation_types(
    kind: BlockKind, lowered: str, source_role: SourceRole
) -> list[ObservationType]:
    found: list[ObservationType] = []
    if kind in {BlockKind.AGREEMENT, BlockKind.CONCLUSION} or lowered.startswith(
        _DECISION_PREFIXES
    ):
        found.append(ObservationType.DECISION)
    if kind == BlockKind.DISCUSSION or lowered.startswith("discussion:"):
        found.append(ObservationType.DISCUSSION_SUMMARY)
    if any(marker in lowered for marker in _OPEN_ISSUE_MARKERS):
        found.append(ObservationType.OPEN_ISSUE)
    if any(marker in lowered for marker in _ACTION_MARKERS):
        found.append(ObservationType.FOLLOW_UP_ACTION)
    if "intended outcome:" in lowered:
        found.append(ObservationType.INTENDED_OUTCOME)
    if "deadline:" in lowered:
        found.append(ObservationType.DEADLINE)
    if any(marker in lowered for marker in _DEPENDENCY_MARKERS):
        found.append(ObservationType.DEPENDENCY)
    if source_role == SourceRole.POST_MEETING_DISCUSSION and not found:
        found.append(ObservationType.DISCUSSION_SUMMARY)
    return list(dict.fromkeys(found))


def _observation_texts(text: str, source_role: SourceRole) -> list[str]:
    if source_role != SourceRole.POST_MEETING_DISCUSSION:
        return [" ".join(text.split()).strip()]
    parts = re.split(r"(?=\[(?:POST|Post|post)\d*\])", text)
    return [" ".join(part.split()).strip() for part in parts if part.strip()]


def _tdoc_ids(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            f"{prefix.upper()}-{number}" for prefix, number in _TDOC_RE.findall(text)
        )
    )


def _conclusion(text: str) -> Conclusion:
    lowered = text.casefold()
    for label, conclusion in sorted(STATUS_ALIASES.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{re.escape(label)}\b", lowered):
            return conclusion
    return Conclusion.UNKNOWN


def _observation_key(
    observation_type: ObservationType,
    agenda_item: str,
    tdoc_ids: list[str],
    text: str,
) -> str:
    anchor = ""
    if not tdoc_ids:
        anchor = re.sub(
            r"\b\d+(?:[./:-]\d+)*\b", "#", _normalize_observation_text(text)
        )
        anchor = " ".join(anchor.split()[:12])
    return _digest(
        "|".join((observation_type, agenda_item, ",".join(sorted(tdoc_ids)), anchor))
    )


def _normalize_observation_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _parse_http_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


async def _persist_observation_graph(
    session: AsyncSession,
    dataset_version_id: str,
    meeting: Meeting,
    observations: list[MeetingObservation],
) -> None:
    node_facts: dict[tuple[str, str], tuple[str, dict[str, str]]] = {
        ("meeting", meeting.id): (
            meeting.name,
            {"working_group": meeting.working_group_id},
        )
    }
    edge_facts: dict[str, tuple[str, str, EdgeType, str, str, list[str]]] = {}
    for item in observations:
        source_id = item.artifact_version_id
        node_facts.setdefault(
            ("source_document", source_id),
            (
                source_id,
                {
                    "source_role": item.source_role,
                    "authority": item.authority,
                },
            ),
        )
        node_facts[("observation", item.id)] = (
            item.text[:500],
            {
                "type": item.observation_type,
                "source_role": item.source_role,
                "authority": item.authority,
            },
        )
        edges = [
            (
                "source_document",
                source_id,
                EdgeType.REPORTS_ON,
                "meeting",
                meeting.id,
            ),
            (
                "source_document",
                source_id,
                EdgeType.CONTAINS,
                "observation",
                item.id,
            ),
            ("meeting", meeting.id, EdgeType.CONTAINS, "observation", item.id),
        ]
        for tdoc_id in item.tdoc_ids:
            node_facts.setdefault(("tdoc", tdoc_id), (tdoc_id, {"placeholder": "true"}))
            edges.append(("observation", item.id, EdgeType.MENTIONS_TDOC, "tdoc", tdoc_id))
            if item.observation_type == ObservationType.DECISION:
                edges.append(
                    ("observation", item.id, EdgeType.CONCLUDES_TDOC, "tdoc", tdoc_id)
                )
        if item.agenda_item:
            agenda_id = f"{meeting.id}:{item.agenda_item}"
            node_facts.setdefault(
                ("agenda_item", agenda_id),
                (item.agenda_item, {"number": item.agenda_item}),
            )
            edges.append(
                (
                    "observation",
                    item.id,
                    EdgeType.BELONGS_TO_AGENDA,
                    "agenda_item",
                    agenda_id,
                )
            )
        for specification in item.specification_ids:
            node_facts.setdefault(("specification", specification), (specification, {}))
            edges.append(
                (
                    "observation",
                    item.id,
                    EdgeType.AFFECTS_SPEC,
                    "specification",
                    specification,
                )
            )
        for edge in edges:
            edge_id = "edge-" + _digest(
                f"{dataset_version_id}|{'|'.join(str(part) for part in edge)}"
            )[:28]
            edge_facts[edge_id] = (*edge, item.evidence_ids)

    identifiers = [identifier for _, identifier in node_facts]
    existing_nodes = {
        (row.entity_type, row.id)
        for row in await session.scalars(
            select(KnowledgeNodeRow).where(
                KnowledgeNodeRow.dataset_version_id == dataset_version_id,
                KnowledgeNodeRow.id.in_(identifiers),
            )
        )
    }
    session.add_all(
        [
            KnowledgeNodeRow(
                dataset_version_id=dataset_version_id,
                entity_type=entity_type,
                id=identifier,
                label=label,
                properties=properties,
            )
            for (entity_type, identifier), (label, properties) in node_facts.items()
            if (entity_type, identifier) not in existing_nodes
        ]
    )
    existing_edges = set(
        await session.scalars(
            select(KnowledgeEdgeRow.id).where(KnowledgeEdgeRow.id.in_(list(edge_facts)))
        )
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
