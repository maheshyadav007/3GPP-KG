from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from threegpp_kg.mcp_server import create_mcp_server, temporal_scope


def test_temporal_scope_defaults_and_rejects_ambiguity() -> None:
    assert temporal_scope(default_last_k=3).last_k_meetings == 3
    assert temporal_scope(date_from=date(2026, 1, 1)).date_from == date(2026, 1, 1)
    with pytest.raises(ValidationError, match="exactly one"):
        temporal_scope(meeting_ids=["RAN2-133"], duration_months=12)
    with pytest.raises(ValueError, match="exactly one"):
        temporal_scope()


@pytest.mark.asyncio
async def test_all_required_mcp_tools_are_registered(service) -> None:
    server = create_mcp_server(service)
    names = {tool.name for tool in await server.list_tools()}
    assert names == {
        "list_meetings",
        "search_topics",
        "search_tdocs",
        "get_tdoc",
        "get_tdoc_sections",
        "get_relevant_passages",
        "get_revision_chain",
        "get_meeting_decisions",
        "get_meeting_brief",
        "list_meeting_sources",
        "get_meeting_source",
        "get_meeting_briefing",
        "get_meeting_changes",
        "get_meeting_timeline",
        "trace_topic",
        "get_hot_topics",
        "get_company_activity",
        "compare_meetings",
        "get_spec_changes",
        "get_newsletter",
        "get_newsletter_packet",
        "get_published_newsletter",
    }


@pytest.mark.asyncio
async def test_passage_result_contains_block_evidence(service) -> None:
    result = await service.relevant_passages("proposal agreed", tdoc_ids=["R2-3"], top_k=3)
    assert result.data[0].block_ids == ["b-1"]
    assert result.evidence[0].id == "ev-1"
    assert result.dataset_version == "test-v1"


@pytest.mark.asyncio
async def test_every_mcp_tool_executes_with_evidence_envelope(service) -> None:
    server = create_mcp_server(service)
    calls = {
        "list_meetings": {"working_groups": ["RAN2"], "meeting_ids": ["RAN2-133"]},
        "search_tdocs": {"query": "carrier", "meeting_ids": ["RAN2-133"]},
        "get_tdoc": {"tdoc_id": "R2-3"},
        "get_tdoc_sections": {"tdoc_id": "R2-3"},
        "get_revision_chain": {"tdoc_id": "R2-2"},
        "get_relevant_passages": {"query": "agreed", "tdoc_ids": ["R2-3"]},
        "get_meeting_decisions": {"meeting_id": "RAN2-133"},
        "get_meeting_brief": {"meeting_id": "RAN2-133"},
        "get_newsletter": {"meeting_id": "RAN2-133", "edition": "final"},
        "get_newsletter_packet": {"meeting_id": "RAN2-133", "edition": "final"},
        "get_published_newsletter": {
            "meeting_id": "RAN2-133",
            "edition": "final",
        },
        "search_topics": {"query": "Carrier aggregation", "meeting_ids": ["RAN2-133"]},
        "trace_topic": {"topic": "Carrier aggregation", "meeting_ids": ["RAN2-133"]},
        "get_hot_topics": {"meeting_id": "RAN2-133"},
        "get_company_activity": {"meeting_id": "RAN2-133"},
        "get_spec_changes": {"specification": "38.306", "meeting_ids": ["RAN2-133"]},
        "compare_meetings": {"meeting_ids": ["RAN2-133", "missing-meeting"]},
    }
    for name, arguments in calls.items():
        _, structured = await server.call_tool(name, arguments)
        assert structured is not None, name
        assert structured["dataset_version"] == "test-v1", name
        assert set(structured) >= {
            "data",
            "evidence",
            "dataset_version",
            "completeness",
            "confidence",
            "warnings",
            "next_cursor",
        }, name

    with pytest.raises(Exception, match="at least two"):
        await server.call_tool("compare_meetings", {"meeting_ids": ["RAN2-133"]})
