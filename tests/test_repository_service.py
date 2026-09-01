from __future__ import annotations

import pytest

from threegpp_kg.constants import Conclusion, MatchMode
from threegpp_kg.domain import RetrievalChunk, SearchFilters, SearchRequest, TemporalScope
from threegpp_kg.models import ModelEndpointError
from threegpp_kg.repository import InMemoryRepository, decode_cursor
from threegpp_kg.service import KnowledgeService


class FakeEmbeddingClient:
    profile_id = "fixture-embedding"
    model_name = "fixture"
    revision = "fixture"
    dimensions = 3

    def __init__(self, *, available: bool = True, fail: bool = False) -> None:
        self._available = available
        self._fail = fail
        self.calls = 0

    def available(self) -> bool:
        return self._available

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self._fail:
            raise ModelEndpointError("injected inference failure")
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.embed_queries(texts)


@pytest.mark.asyncio
async def test_search_returns_evidence_and_dataset_version(service) -> None:
    result = await service.search_tdocs(SearchRequest(query="carrier", top_k=10))
    assert [item.id for item in result.data] == ["R2-1", "R2-2", "R2-3"]
    assert result.dataset_version == "test-v1"
    assert [item.id for item in result.evidence] == ["ev-1"]


@pytest.mark.asyncio
async def test_and_or_company_filters(service) -> None:
    all_result = await service.search_tdocs(
        SearchRequest(
            filters=SearchFilters(companies=["Qualcomm", "Ericsson"], match_mode=MatchMode.ALL)
        )
    )
    any_result = await service.search_tdocs(
        SearchRequest(
            filters=SearchFilters(companies=["Qualcomm", "Ericsson"], match_mode=MatchMode.ANY)
        )
    )
    assert [item.id for item in all_result.data] == ["R2-2", "R2-3"]
    assert [item.id for item in any_result.data] == ["R2-1", "R2-2", "R2-3"]


@pytest.mark.asyncio
async def test_status_and_temporal_filters(service) -> None:
    result = await service.search_tdocs(
        SearchRequest(
            filters=SearchFilters(
                statuses=[Conclusion.AGREED],
                temporal=TemporalScope(meeting_ids=["RAN2-133"]),
            )
        )
    )
    assert [item.id for item in result.data] == ["R2-3"]


@pytest.mark.asyncio
async def test_revision_chain_is_ordered(service) -> None:
    result = await service.revision_chain("R2-2")
    assert result.data == ["R2-1", "R2-2", "R2-3"]


@pytest.mark.asyncio
async def test_newsletter_packet_is_deterministic(service) -> None:
    result = await service.newsletter_packet("RAN2-133", "final")
    assert result.data is not None
    assert result.data.totals["tdocs"] == 3
    assert result.data.totals["agreed"] == 1
    assert result.data.company_activity[0] == {"company": "Qualcomm", "tdoc_count": 3}
    assert result.data.evidence_ids == ["ev-1"]


@pytest.mark.asyncio
async def test_document_section_tree_has_stable_navigation_anchors(service) -> None:
    result = await service.document_section_tree("R2-3")

    assert [node["title"] for node in result.data] == ["Document", "Conclusion"]
    root, conclusion = result.data
    assert root["parent_id"] is None
    assert root["child_count"] == 1
    assert root["descendant_block_count"] == 1
    assert conclusion["parent_id"] == root["id"]
    assert conclusion["start_block_index"] == 0
    assert conclusion["direct_block_count"] == 1


def semantic_service(
    service: KnowledgeService, *, model_available: bool = True, model_failure: bool = False
) -> tuple[KnowledgeService, FakeEmbeddingClient]:
    repository = service.repository
    assert isinstance(repository, InMemoryRepository)
    chunks = [
        RetrievalChunk.model_validate({**chunk.model_dump(), "embedding": [1.0, 0.0, 0.0]})
        for chunk in repository.chunks
    ]
    client = FakeEmbeddingClient(available=model_available, fail=model_failure)
    return (
        KnowledgeService(
            InMemoryRepository(
                repository.meetings,
                repository.tdocs,
                list(repository.evidence_map.values()),
                chunks,
                repository.blocks,
                repository.dataset_version,
            ),
            client,
        ),
        client,
    )


@pytest.mark.asyncio
async def test_relevant_passages_reports_active_hybrid_profile(service) -> None:
    semantic, client = semantic_service(service)
    result = await semantic.relevant_passages("carrier proposal")

    assert result.retrieval is not None
    assert result.retrieval.mode == "hybrid"
    assert result.retrieval.embedding_profile is not None
    assert result.retrieval.embedding_profile.id == "fixture-embedding"
    assert result.warnings == []
    assert client.calls == 1
    health = await semantic.semantic_health()
    assert health["ready"] is True
    assert health["coverage"] == 1.0


@pytest.mark.asyncio
async def test_relevant_passages_explicitly_reports_lexical_fallback(service) -> None:
    semantic, client = semantic_service(service, model_available=False)
    result = await semantic.relevant_passages("carrier proposal")

    assert result.data
    assert result.retrieval is not None
    assert result.retrieval.mode == "lexical_fallback"
    assert result.retrieval.embedding_profile is None
    assert "unavailable" in result.warnings[0]
    assert client.calls == 0


@pytest.mark.asyncio
async def test_relevant_passages_falls_back_after_inference_failure(service) -> None:
    semantic, client = semantic_service(service, model_failure=True)
    result = await semantic.relevant_passages("carrier proposal")

    assert result.data
    assert result.retrieval is not None
    assert result.retrieval.mode == "lexical_fallback"
    assert "failed" in result.warnings[0]
    assert client.calls == 1


def test_invalid_cursor_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor("not-base64")
