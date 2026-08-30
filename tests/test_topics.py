from __future__ import annotations

from typing import Any

import pytest

from threegpp_kg.config import FeatureConfig
from threegpp_kg.constants import EvidenceAuthority
from threegpp_kg.domain import EvidenceRef
from threegpp_kg.topics import TopicExtractor


class FakeTopicClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def chat_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((args, kwargs))
        return self.response


def evidence() -> list[EvidenceRef]:
    return [
        EvidenceRef(
            id="ev-1",
            source_url="https://www.3gpp.org/doc",
            artifact_sha256="a" * 64,
            authority=EvidenceAuthority.TDOC_BODY,
        )
    ]


@pytest.mark.asyncio
async def test_topic_extraction_disabled_is_explicit() -> None:
    result = await TopicExtractor(FeatureConfig(), None).extract("text", evidence(), "v1")
    assert result.data == []
    assert result.completeness == "unavailable"
    assert result.confidence == 0
    assert result.warnings == ["Topic extraction is disabled"]
    assert result.dataset_version == "v1"
    assert result.evidence == evidence()


@pytest.mark.asyncio
async def test_topic_extraction_merges_aliases_and_applies_threshold() -> None:
    client = FakeTopicClient(
        {
            "topics": [
                {
                    "canonical_name": "Carrier aggregation",
                    "aliases": ["CA"],
                    "confidence": 0.9,
                    "evidence_ids": ["ev-1"],
                },
                {
                    "canonical_name": "carrier aggregation",
                    "aliases": ["Multi-carrier"],
                    "confidence": 0.8,
                    "evidence_ids": ["ev-1"],
                },
                {
                    "canonical_name": "Unsupported guess",
                    "confidence": 0.2,
                    "evidence_ids": ["ev-1"],
                },
            ]
        }
    )
    extractor = TopicExtractor(
        FeatureConfig(topic_extraction_enabled=True, topic_min_confidence=0.65),
        client,  # type: ignore[arg-type]
    )
    result = await extractor.extract("carrier text", evidence(), "v1")
    assert len(result.data) == 1
    assert result.data[0].canonical_name == "Carrier aggregation"
    assert result.data[0].aliases == ["CA", "Multi-carrier"]
    assert result.data[0].confidence == 0.9
    assert result.data[0].evidence_ids == ["ev-1"]
    assert result.confidence == 0.9
    assert result.completeness == "complete"
    args, kwargs = client.calls[0]
    assert args[0][0]["role"] == "system"
    assert "supported by the supplied evidence" in args[0][0]["content"]
    assert args[0][1] == {"role": "user", "content": "carrier text"}
    assert kwargs["schema_name"] == "threegpp_topics"
    assert kwargs["schema"]["required"] == ["topics"]


@pytest.mark.asyncio
async def test_topic_extraction_rejects_unknown_evidence() -> None:
    client = FakeTopicClient(
        {
            "topics": [
                {
                    "canonical_name": "Fabricated",
                    "confidence": 0.9,
                    "evidence_ids": ["unknown"],
                }
            ]
        }
    )
    extractor = TopicExtractor(
        FeatureConfig(topic_extraction_enabled=True),
        client,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="unsupported evidence"):
        await extractor.extract("text", evidence(), "v1")
