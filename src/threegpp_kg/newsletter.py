from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from .config import FeatureConfig
from .domain import Envelope, NewsletterPacket
from .models.client import OpenAICompatibleClient


class RenderedClaim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(min_length=1)
    organizations: list[str] = Field(default_factory=list)


class RenderedNewsletter(BaseModel):
    title: str
    sections: list[dict[str, Any]]
    claims: list[RenderedClaim]


class NewsletterRenderer:
    def __init__(
        self,
        features: FeatureConfig,
        client: OpenAICompatibleClient | None,
    ) -> None:
        self.features = features
        self.client = client

    async def render(
        self, packet_envelope: Envelope[NewsletterPacket | None]
    ) -> Envelope[RenderedNewsletter | None]:
        if packet_envelope.data is None:
            return Envelope(
                data=None,
                dataset_version=packet_envelope.dataset_version,
                completeness="unavailable",
                confidence=0,
                warnings=packet_envelope.warnings,
            )
        if packet_envelope.completeness != "complete":
            raise ValueError("newsletter publication requires a complete briefing packet")
        if not self.features.newsletter_generation_enabled or not self.client:
            return Envelope(
                data=None,
                evidence=packet_envelope.evidence,
                dataset_version=packet_envelope.dataset_version,
                completeness="unavailable",
                confidence=0,
                warnings=["Newsletter prose generation is disabled; use the briefing packet"],
            )
        schema = RenderedNewsletter.model_json_schema()
        raw = await self.client.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "Render only facts present in the packet. Every claim must cite "
                        "evidence IDs."
                    ),
                },
                {"role": "user", "content": packet_envelope.data.model_dump_json()},
            ],
            schema_name="threegpp_newsletter",
            schema=schema,
        )
        rendered = RenderedNewsletter.model_validate(raw)
        allowed = set(packet_envelope.data.evidence_ids)
        invalid = [
            evidence_id
            for claim in rendered.claims
            for evidence_id in claim.evidence_ids
            if evidence_id not in allowed
        ]
        if invalid:
            raise ValueError(
                f"newsletter contains unsupported evidence IDs: {sorted(set(invalid))}"
            )
        packet_text = packet_envelope.data.model_dump_json()
        allowed_numbers = set(re.findall(r"\b\d+(?:\.\d+)*\b", packet_text))
        unsupported_numbers = {
            value
            for claim in rendered.claims
            for value in re.findall(r"\b\d+(?:\.\d+)*\b", claim.text)
            if value not in allowed_numbers
        }
        if unsupported_numbers:
            raise ValueError(
                f"newsletter contains unsupported numbers: {sorted(unsupported_numbers)}"
            )
        allowed_organizations = {
            str(item["company"]) for item in packet_envelope.data.company_activity
        }
        unsupported_organizations = {
            organization
            for claim in rendered.claims
            for organization in claim.organizations
            if organization not in allowed_organizations or organization not in claim.text
        }
        if unsupported_organizations:
            raise ValueError(
                "newsletter contains unsupported organization attribution: "
                f"{sorted(unsupported_organizations)}"
            )
        return Envelope(
            data=rendered,
            evidence=packet_envelope.evidence,
            dataset_version=packet_envelope.dataset_version,
        )
