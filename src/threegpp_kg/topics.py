from __future__ import annotations

from pydantic import BaseModel, Field

from .config import FeatureConfig
from .domain import Envelope, EvidenceRef
from .models.client import OpenAICompatibleClient


class ExtractedTopic(BaseModel):
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)


class TopicExtractionResponse(BaseModel):
    topics: list[ExtractedTopic]


class TopicExtractor:
    def __init__(
        self,
        features: FeatureConfig,
        client: OpenAICompatibleClient | None,
    ) -> None:
        self.features = features
        self.client = client

    async def extract(
        self,
        text: str,
        evidence: list[EvidenceRef],
        dataset_version: str,
    ) -> Envelope[list[ExtractedTopic]]:
        if not self.features.topic_extraction_enabled or self.client is None:
            return Envelope(
                data=[],
                evidence=evidence,
                dataset_version=dataset_version,
                completeness="unavailable",
                confidence=0,
                warnings=["Topic extraction is disabled"],
            )
        schema = TopicExtractionResponse.model_json_schema()
        raw = await self.client.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "Extract only technical topics supported by the supplied evidence. "
                        "Return canonical names, aliases, confidence, and evidence IDs."
                    ),
                },
                {"role": "user", "content": text},
            ],
            schema_name="threegpp_topics",
            schema=schema,
        )
        response = TopicExtractionResponse.model_validate(raw)
        allowed = {item.id for item in evidence}
        merged: dict[str, ExtractedTopic] = {}
        for topic in response.topics:
            if topic.confidence < self.features.topic_min_confidence:
                continue
            if not set(topic.evidence_ids).issubset(allowed):
                raise ValueError(f"topic {topic.canonical_name!r} has unsupported evidence")
            key = topic.canonical_name.casefold()
            existing = merged.get(key)
            if existing is None:
                merged[key] = topic
                continue
            merged[key] = ExtractedTopic(
                canonical_name=existing.canonical_name,
                aliases=list(dict.fromkeys([*existing.aliases, *topic.aliases])),
                confidence=max(existing.confidence, topic.confidence),
                evidence_ids=list(dict.fromkeys([*existing.evidence_ids, *topic.evidence_ids])),
            )
        topics = sorted(merged.values(), key=lambda item: item.canonical_name.casefold())
        return Envelope(
            data=topics,
            evidence=evidence,
            dataset_version=dataset_version,
            confidence=min((item.confidence for item in topics), default=0),
            completeness="complete",
        )
