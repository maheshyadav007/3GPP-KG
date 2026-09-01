from __future__ import annotations

from datetime import date

import pytest

from threegpp_kg.constants import BlockKind, Conclusion, EvidenceAuthority
from threegpp_kg.domain import DocumentBlock, EvidenceRef, Meeting, RetrievalChunk, TDoc
from threegpp_kg.repository import InMemoryRepository
from threegpp_kg.service import KnowledgeService


@pytest.fixture
def service() -> KnowledgeService:
    meeting = Meeting(
        id="RAN2-133",
        working_group_id="RAN2",
        number=133,
        name="RAN2 #133",
        source_url="https://www.3gpp.org/meeting",
        starts_on=date(2026, 5, 4),
        ends_on=date(2026, 5, 8),
        readiness="final_ready",
    )
    evidence = EvidenceRef(
        id="ev-1",
        source_url="https://www.3gpp.org/report",
        artifact_sha256="a" * 64,
        authority=EvidenceAuthority.FINAL_REPORT,
        meeting_id=meeting.id,
        tdoc_id="R2-3",
        block_id="b-1",
        excerpt="Agreed",
    )
    tdocs = [
        TDoc(
            id="R2-1",
            meeting_id=meeting.id,
            title="Carrier aggregation proposal",
            source="Qualcomm",
            status=Conclusion.REVISED,
            revised_from="R2-0",
            revised_to="R2-2",
            releases=["Rel-20"],
            agenda_description="Carrier aggregation",
            evidence_ids=["ev-1"],
        ),
        TDoc(
            id="R2-2",
            meeting_id=meeting.id,
            title="Carrier aggregation proposal",
            source="Qualcomm, Ericsson",
            status=Conclusion.REVISED,
            revised_from="R2-1",
            revised_to="R2-3",
            releases=["Rel-20"],
            agenda_description="Carrier aggregation",
            evidence_ids=["ev-1"],
        ),
        TDoc(
            id="R2-3",
            meeting_id=meeting.id,
            title="Carrier aggregation proposal",
            source="Qualcomm, Ericsson",
            status=Conclusion.AGREED,
            revised_from="R2-2",
            releases=["Rel-20"],
            specifications=["38.306"],
            work_items=["NR_CA_Ph2"],
            cr_number="0123",
            cr_revision="1",
            agenda_description="Carrier aggregation",
            evidence_ids=["ev-1"],
        ),
    ]
    chunks = [
        RetrievalChunk(
            id="chunk-1",
            document_id="R2-3",
            block_ids=["b-1"],
            text="The carrier aggregation proposal was agreed.",
            section_path=["Conclusion"],
            token_count=8,
            evidence_ids=["ev-1"],
        )
    ]
    blocks = [
        DocumentBlock(
            id="b-1",
            document_id="R2-3",
            index=0,
            kind=BlockKind.CONCLUSION,
            text="The carrier aggregation proposal was agreed.",
            section_path=["Conclusion"],
        )
    ]
    return KnowledgeService(
        InMemoryRepository([meeting], tdocs, [evidence], chunks, blocks, dataset_version="test-v1")
    )


@pytest.fixture
def multi_meeting_service(service: KnowledgeService) -> KnowledgeService:
    repository = service.repository
    assert isinstance(repository, InMemoryRepository)
    previous_meeting = Meeting(
        id="RAN2-132",
        working_group_id="RAN2",
        number=132,
        name="RAN2 #132",
        source_url="https://www.3gpp.org/meeting-132",
        starts_on=date(2026, 2, 16),
        ends_on=date(2026, 2, 20),
        readiness="final_ready",
    )
    predecessor = TDoc(
        id="R2-0",
        meeting_id=previous_meeting.id,
        title="Initial carrier aggregation proposal",
        source="Qualcomm",
        status=Conclusion.REVISED,
        revised_to="R2-1",
        releases=["Rel-20"],
        agenda_description="Carrier aggregation",
    )
    return KnowledgeService(
        InMemoryRepository(
            [*repository.meetings, previous_meeting],
            [*repository.tdocs, predecessor],
            list(repository.evidence_map.values()),
            repository.chunks,
            repository.blocks,
            dataset_version=repository.dataset_version,
        )
    )
