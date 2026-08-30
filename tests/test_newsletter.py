from __future__ import annotations

from typing import Any

import pytest

from threegpp_kg.config import FeatureConfig
from threegpp_kg.domain import Envelope, NewsletterPacket
from threegpp_kg.newsletter import NewsletterRenderer


class FakeModelClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def chat_json(self, *args, **kwargs) -> dict[str, Any]:
        self.calls.append((args, kwargs))
        return self.response


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
    client = FakeModelClient(
        {
            "title": "RAN2 update",
            "sections": [],
            "claims": [{"text": "Unsupported", "evidence_ids": ["invented"]}],
        }
    )
    renderer = NewsletterRenderer(FeatureConfig(newsletter_generation_enabled=True), client)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported evidence"):
        await renderer.render(packet)


@pytest.mark.asyncio
async def test_renderer_accepts_cited_claims(service) -> None:
    packet = await service.newsletter_packet("RAN2-133")
    client = FakeModelClient(
        {
            "title": "RAN2 update",
            "sections": [{"title": "Decisions", "body": "One item agreed."}],
            "claims": [{"text": "One item agreed", "evidence_ids": ["ev-1"]}],
        }
    )
    renderer = NewsletterRenderer(FeatureConfig(newsletter_generation_enabled=True), client)  # type: ignore[arg-type]
    result = await renderer.render(packet)
    assert result.data and result.data.claims[0].evidence_ids == ["ev-1"]
    assert result.dataset_version == packet.dataset_version
    assert result.evidence == packet.evidence
    args, kwargs = client.calls[0]
    assert args[0][0]["role"] == "system"
    assert "Every claim must cite evidence IDs" in args[0][0]["content"]
    assert packet.data is not None
    assert args[0][1]["content"] == packet.data.model_dump_json()
    assert kwargs["schema_name"] == "threegpp_newsletter"
    assert kwargs["schema"]["required"] == ["title", "sections", "claims"]


@pytest.mark.asyncio
async def test_renderer_rejects_unsupported_numbers_and_organizations(service) -> None:
    packet = await service.newsletter_packet("RAN2-133")
    number_client = FakeModelClient(
        {
            "title": "RAN2 update",
            "sections": [],
            "claims": [{"text": "999 items agreed", "evidence_ids": ["ev-1"]}],
        }
    )
    renderer = NewsletterRenderer(
        FeatureConfig(newsletter_generation_enabled=True),
        number_client,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="unsupported numbers"):
        await renderer.render(packet)

    organization_client = FakeModelClient(
        {
            "title": "RAN2 update",
            "sections": [],
            "claims": [
                {
                    "text": "UnknownCo submitted the item",
                    "evidence_ids": ["ev-1"],
                    "organizations": ["UnknownCo"],
                }
            ],
        }
    )
    renderer = NewsletterRenderer(
        FeatureConfig(newsletter_generation_enabled=True),
        organization_client,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="organization attribution"):
        await renderer.render(packet)
