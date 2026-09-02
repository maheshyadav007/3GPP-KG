from __future__ import annotations

import json
from typing import Any

import pytest

from threegpp_kg.config import FeatureConfig, NewsletterConfig
from threegpp_kg.constants import Conclusion, NewsletterStatus
from threegpp_kg.domain import Envelope, NewsletterPacket, TDoc
from threegpp_kg.newsletter import NewsletterRenderer
from threegpp_kg.repository import InMemoryRepository
from threegpp_kg.service import KnowledgeService

SECTION_KINDS = [
    "material_changes",
    "decisions",
    "topic_evolution",
    "technical_impact",
    "company_activity",
    "engineering_implications",
    "watch_items",
    "appendix_summary",
]


class FakeModelClient:
    model_name = "Qwen/Qwen3-32B"
    revision = "endpoint-managed"

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def chat_json(self, *args, **kwargs) -> dict[str, Any]:
        self.calls.append((args, kwargs))
        return self.response

    async def close(self) -> None:
        return None


def response(
    text: str = "Carrier aggregation was agreed",
    *,
    evidence_ids: list[str] | None = None,
    organizations: list[str] | None = None,
    specifications: list[str] | None = None,
    conclusions: list[str] | None = None,
) -> dict[str, Any]:
    paragraph = {
        "text": text,
        "evidence_ids": evidence_ids or ["ev-1"],
        "organizations": organizations if organizations is not None else [],
        "specifications": specifications if specifications is not None else [],
        "conclusions": conclusions if conclusions is not None else ["agreed"],
    }
    return {
        "title": "RAN2 analytical update",
        "executive_summary": [paragraph],
        "sections": [
            {"kind": kind, "title": kind.replace("_", " ").title(), "paragraphs": []}
            for kind in SECTION_KINDS
        ],
    }


@pytest.mark.asyncio
async def test_renderer_rejects_absent_or_incomplete_packet(service) -> None:
    absent = Envelope[NewsletterPacket | None](
        data=None,
        dataset_version="dataset-1",
        completeness="unavailable",
        confidence=0,
        warnings=["No meeting found"],
    )
    result = await NewsletterRenderer(FeatureConfig(), None).render(absent)
    assert result.data is None
    assert result.dataset_version == "dataset-1"
    assert result.warnings == ["No meeting found"]

    packet = await service.newsletter_packet("RAN2-133")
    incomplete = packet.model_copy(update={"completeness": "partial"})
    with pytest.raises(ValueError, match="complete briefing packet"):
        await NewsletterRenderer(FeatureConfig(), None).render(incomplete)


@pytest.mark.asyncio
async def test_disabled_newsletter_returns_explicit_status(service) -> None:
    packet = await service.newsletter_packet("RAN2-133")
    result = await NewsletterRenderer(FeatureConfig(), None).render(packet)
    assert result.data is None
    assert result.completeness == "unavailable"
    assert result.confidence == 0
    assert result.warnings == ["Newsletter prose generation is disabled; use the briefing packet"]
    assert result.evidence == packet.evidence


@pytest.mark.asyncio
async def test_renderer_rejects_unknown_evidence(service) -> None:
    packet = await service.newsletter_packet("RAN2-133")
    client = FakeModelClient(response(evidence_ids=["invented"]))
    renderer = NewsletterRenderer(FeatureConfig(newsletter_generation_enabled=True), client)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported evidence"):
        await renderer.render(packet)


@pytest.mark.asyncio
async def test_renderer_accepts_cited_paragraphs(service) -> None:
    packet = await service.newsletter_packet("RAN2-133")
    client = FakeModelClient(response())
    renderer = NewsletterRenderer(FeatureConfig(newsletter_generation_enabled=True), client)  # type: ignore[arg-type]
    result = await renderer.render(packet)
    assert result.data and result.data.executive_summary[0].evidence_ids == ["ev-1"]
    assert result.dataset_version == packet.dataset_version
    assert result.evidence == packet.evidence
    args, kwargs = client.calls[0]
    assert args[0][0]["role"] == "system"
    assert "Every paragraph" in args[0][0]["content"]
    payload = json.loads(args[0][1]["content"])
    assert packet.data is not None
    assert payload["packet_id"] == packet.data.id
    assert "tdoc_appendix" not in payload
    assert kwargs["schema_name"] == "threegpp_wg_newsletter_v1"
    assert kwargs["schema"]["required"] == ["title", "executive_summary", "sections"]


@pytest.mark.asyncio
async def test_renderer_rejects_unsupported_numbers_and_organizations(service) -> None:
    packet = await service.newsletter_packet("RAN2-133")
    renderer = NewsletterRenderer(
        FeatureConfig(newsletter_generation_enabled=True),
        FakeModelClient(response("999 items were agreed")),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="unsupported numbers"):
        await renderer.render(packet)

    renderer = NewsletterRenderer(
        FeatureConfig(newsletter_generation_enabled=True),
        FakeModelClient(response("UnknownCo submitted the item", organizations=["UnknownCo"])),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="organization attribution"):
        await renderer.render(packet)


@pytest.mark.asyncio
async def test_renderer_rejects_missing_sections_and_undeclared_attribution(service) -> None:
    packet = await service.newsletter_packet("RAN2-133")
    missing = response()
    missing["sections"] = missing["sections"][:-1]
    renderer = NewsletterRenderer(
        FeatureConfig(newsletter_generation_enabled=True),
        FakeModelClient(missing),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="at least 8"):
        await renderer.render(packet)

    renderer = NewsletterRenderer(
        FeatureConfig(newsletter_generation_enabled=True),
        FakeModelClient(response("Qualcomm submitted an agreed proposal")),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="organization attribution"):
        await renderer.render(packet)


@pytest.mark.asyncio
async def test_packet_processes_every_tdoc_without_search_cap(service) -> None:
    repository = service.repository
    assert isinstance(repository, InMemoryRepository)
    generated = [
        TDoc(
            id=f"R2-X{index:03d}",
            meeting_id="RAN2-133",
            title=f"Additional proposal {index}",
            source="Ericsson",
            status=Conclusion.NOTED,
            agenda_description="Additional proposals",
            evidence_ids=["ev-1"],
        )
        for index in range(125)
    ]
    expanded = KnowledgeService(
        InMemoryRepository(
            repository.meetings,
            [*repository.tdocs, *generated],
            list(repository.evidence_map.values()),
            repository.chunks,
            repository.blocks,
            repository.dataset_version,
        )
    )
    packet = await expanded.newsletter_packet("RAN2-133")
    assert packet.data is not None
    assert packet.data.totals["tdocs"] == 128
    assert len(packet.data.tdoc_appendix) == 128


@pytest.mark.asyncio
async def test_packet_traces_cross_meeting_revision_and_topic_acceleration(
    multi_meeting_service,
) -> None:
    packet = await multi_meeting_service.newsletter_packet("RAN2-133")
    assert packet.data is not None
    assert packet.data.comparison_window == 2
    assert packet.data.revision_analysis[0].chain == ["R2-0", "R2-1", "R2-2", "R2-3"]
    trend = next(item for item in packet.data.topic_trends if item.topic == "Carrier aggregation")
    assert trend.classification == "accelerating"
    assert trend.counts_by_meeting == {"RAN2-132": 1, "RAN2-133": 3}
    assert packet.data.engineering_implications


@pytest.mark.asyncio
async def test_repeated_unsuccessful_proposals_become_watch_items(
    multi_meeting_service,
) -> None:
    repository = multi_meeting_service.repository
    assert isinstance(repository, InMemoryRepository)
    revised = [
        item.model_copy(update={"status": Conclusion.REJECTED})
        if item.id in {"R2-0", "R2-1"}
        else item
        for item in repository.tdocs
    ]
    service = KnowledgeService(
        InMemoryRepository(
            repository.meetings,
            revised,
            list(repository.evidence_map.values()),
            repository.chunks,
            repository.blocks,
            repository.dataset_version,
        )
    )
    packet = await service.newsletter_packet("RAN2-133")
    assert packet.data is not None
    assert any(item.category == "repeated_unsuccessful" for item in packet.data.watch_items)


@pytest.mark.asyncio
async def test_generation_is_persisted_idempotently_and_requires_review(service) -> None:
    repository = service.repository
    generated_service = KnowledgeService(
        repository,
        newsletter_config=NewsletterConfig(require_human_approval=True),
        generation_client=FakeModelClient(response()),  # type: ignore[arg-type]
        features=FeatureConfig(newsletter_generation_enabled=True),
    )
    first = await generated_service.build_newsletter("RAN2-133", render=True)
    assert first.data is not None
    assert first.data.status == NewsletterStatus.PENDING_APPROVAL
    assert first.data.rendered is not None
    second = await generated_service.build_newsletter("RAN2-133", render=False)
    assert second.data is not None
    assert second.data.id == first.data.id
    assert second.data.rendered_sha256 == first.data.rendered_sha256

    approved = await generated_service.review_newsletter(
        first.data.id, "approved", "reviewer@example.com", "Verified against evidence"
    )
    assert approved.data is not None
    assert approved.data.status == NewsletterStatus.APPROVED
    published = await generated_service.get_newsletter_record("RAN2-133", approved_only=True)
    assert published.data and published.data.id == first.data.id

    final = await generated_service.build_newsletter("RAN2-133", "final", render=False)
    assert final.data and final.data.packet.provisional_to_final is not None
    assert final.data.packet.provisional_to_final.provisional_packet_id == first.data.packet.id


@pytest.mark.asyncio
async def test_failed_generation_is_retained_for_audit(service) -> None:
    repository = service.repository
    generated_service = KnowledgeService(
        repository,
        generation_client=FakeModelClient(response(evidence_ids=["invented"])),  # type: ignore[arg-type]
        features=FeatureConfig(newsletter_generation_enabled=True),
    )
    with pytest.raises(ValueError, match="unsupported evidence"):
        await generated_service.build_newsletter("RAN2-133", render=True)
    record = await repository.latest_newsletter("RAN2-133", "provisional")
    assert record is not None
    assert record.status == NewsletterStatus.GENERATION_FAILED
    assert record.generation_error and "unsupported evidence" in record.generation_error
