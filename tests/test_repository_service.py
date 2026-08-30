from __future__ import annotations

import pytest

from threegpp_kg.constants import Conclusion, MatchMode
from threegpp_kg.domain import SearchFilters, SearchRequest, TemporalScope
from threegpp_kg.repository import decode_cursor


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


def test_invalid_cursor_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor("not-base64")
