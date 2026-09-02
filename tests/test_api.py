from __future__ import annotations

import httpx
import pytest

from threegpp_kg.api import create_app


@pytest.mark.asyncio
async def test_health_and_evidence_api(service) -> None:
    app = create_app(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        health = await client.get("/health")
        assert health.json() == {
            "status": "ok",
            "dataset_version": "test-v1",
            "semantic_search": {
                "active": False,
                "profile": None,
                "coverage": 0.0,
                "configured": False,
                "model_cached": False,
                "profile_matches_config": False,
                "ready": False,
            },
        }
        response = await client.get("/api/tdocs/R2-3")
        assert response.status_code == 200
        assert response.json()["evidence"][0]["id"] == "ev-1"
        too_large = await client.get("/api/tdocs/R2-3", params={"block_limit": 2001})
        assert too_large.status_code == 422

        sections = await client.get("/api/tdocs/R2-3/sections")
        assert sections.status_code == 200
        assert sections.json()["data"][1]["title"] == "Conclusion"
        section_limit = await client.get("/api/tdocs/R2-3/sections", params={"limit": 1001})
        assert section_limit.status_code == 422


@pytest.mark.asyncio
async def test_graph_is_bounded_and_contains_typed_nodes(service) -> None:
    app = create_app(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/graph", params={"query": "carrier", "limit": 20})
        assert response.status_code == 200
        body = response.json()
        assert {
            "tdoc",
            "company",
            "topic",
            "meeting",
            "specification",
            "release",
        } <= {node["type"] for node in body["data"]["nodes"]}
        node_ids = {node["id"] for node in body["data"]["nodes"]}
        assert all(
            edge["source"] in node_ids and edge["target"] in node_ids
            for edge in body["data"]["edges"]
        )
        too_large = await client.get("/api/graph", params={"limit": 1001})
        assert too_large.status_code == 422

        balanced = await client.get(
            "/api/graph", params=[("working_groups", "RAN2"), ("limit", "100")]
        )
        assert balanced.status_code == 200
        assert {
            node["label"] for node in balanced.json()["data"]["nodes"] if node["type"] == "meeting"
        } == {"RAN2-133"}


@pytest.mark.asyncio
async def test_search_meetings_newsletter_and_missing_document_routes(service) -> None:
    app = create_app(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        search = await client.post("/api/tdocs/search", json={"query": "carrier", "top_k": 2})
        assert search.status_code == 200
        assert len(search.json()["data"]) == 2

        meetings = await client.get(
            "/api/meetings", params={"working_group": "RAN2", "last_k_meetings": 1}
        )
        assert meetings.status_code == 200
        assert meetings.json()["data"][0]["id"] == "RAN2-133"
        assert meetings.json()["data"][0]["tdoc_count"] == 3

        sources = await client.get("/api/meetings/RAN2-133/sources")
        assert sources.status_code == 200
        assert sources.json()["data"] == []
        briefing = await client.get("/api/meetings/RAN2-133/briefing")
        assert briefing.status_code == 200
        assert briefing.json()["data"]["meeting"]["id"] == "RAN2-133"
        missing_source = await client.get(
            "/api/meetings/RAN2-133/source-content",
            params={"document_id": "missing"},
        )
        assert missing_source.status_code == 404

        newsletter = await client.get("/api/newsletters/RAN2-133", params={"edition": "final"})
        assert newsletter.status_code == 200
        assert newsletter.json()["data"]["edition"] == "final"

        generated = await client.post(
            "/api/newsletters/RAN2-133/generate",
            params={"edition": "provisional"},
        )
        assert generated.status_code == 200
        assert generated.json()["data"]["status"] == "packet_ready"
        record = await client.get("/api/newsletters/RAN2-133/record")
        assert record.status_code == 200
        assert record.json()["data"]["packet"]["totals"]["tdocs"] == 3
        cannot_review = await client.post(
            f"/api/newsletter-reviews/{record.json()['data']['id']}",
            params={"decision": "approved", "reviewer": "architect@example.com"},
        )
        assert cannot_review.status_code == 409

        assert (await client.get("/api/tdocs/missing")).status_code == 404
        assert (await client.get("/api/newsletters/missing")).status_code == 404
        assert (await client.get("/api/newsletters/missing/record")).status_code == 404


@pytest.mark.asyncio
async def test_complete_meeting_graph_and_scoped_facets(service) -> None:
    app = create_app(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/meetings/RAN2-133/graph")
        assert response.status_code == 200
        body = response.json()
        assert body["completeness"] == "complete"
        assert body["data"]["counts"]["tdocs"] == 3
        assert body["data"]["counts"]["tdocs"] == body["data"]["counts"]["total_tdocs"]
        node_types = {node["type"] for node in body["data"]["nodes"]}
        assert {
            "meeting",
            "tdoc",
            "organization",
            "agenda_item",
            "topic",
            "specification",
            "release",
            "work_item",
            "change_request",
        } <= node_types
        node_ids = {node["id"] for node in body["data"]["nodes"]}
        assert all(
            edge["source"] in node_ids and edge["target"] in node_ids
            for edge in body["data"]["edges"]
        )
        boundary = next(node for node in body["data"]["nodes"] if node["id"] == "tdoc:R2-0")
        assert boundary["boundary"] is True
        assert body["evidence"][0]["id"] == "ev-1"

        companies = await client.get("/api/meetings/RAN2-133/facets/company", params={"q": "er"})
        assert companies.status_code == 200
        assert companies.json()["data"] == [
            {"id": "ericsson", "label": "Ericsson", "tdoc_count": 2}
        ]
        topics = await client.get("/api/meetings/RAN2-133/facets/topic")
        assert topics.json()["data"][0]["label"] == "Carrier aggregation"

        filtered = await client.get(
            "/api/meetings/RAN2-133/graph",
            params=[("company_ids", "ericsson"), ("specification_ids", "38.306")],
        )
        assert filtered.status_code == 200
        assert filtered.json()["data"]["counts"]["tdocs"] == 1
        assert filtered.json()["data"]["counts"]["total_tdocs"] == 3

        any_filtered = await client.get(
            "/api/meetings/RAN2-133/graph",
            params=[
                ("company_ids", "ericsson"),
                ("specification_ids", "not-present"),
                ("match_mode", "any"),
            ],
        )
        assert any_filtered.json()["data"]["counts"]["tdocs"] == 2
        assert (await client.get("/api/meetings/missing/graph")).status_code == 404
        assert (await client.get("/api/meetings/RAN2-133/facets/unsupported")).status_code == 422


@pytest.mark.asyncio
async def test_complete_working_group_graph_resolves_cross_meeting_revisions(
    multi_meeting_service,
) -> None:
    app = create_app(multi_meeting_service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/working-groups/RAN2/graph")
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["scope"] == {
            "type": "working_group",
            "id": "RAN2",
            "label": "RAN2",
        }
        assert body["data"]["counts"]["meetings"] == 2
        assert body["data"]["counts"]["tdocs"] == 4
        assert body["data"]["revision_stats"]["cross_meeting_edges"] == 1
        assert body["data"]["revision_stats"]["longest_chain"] == {
            "length": 4,
            "tdoc_ids": ["R2-0", "R2-1", "R2-2", "R2-3"],
            "meeting_ids": ["RAN2-132", "RAN2-133"],
        }
        predecessor = next(node for node in body["data"]["nodes"] if node["id"] == "tdoc:R2-0")
        assert predecessor["boundary"] is False
        highlighted = [
            edge
            for edge in body["data"]["edges"]
            if edge["type"] == "revises" and edge["highlighted"]
        ]
        assert len(highlighted) == 3

        companies = await client.get("/api/working-groups/RAN2/facets/company", params={"q": "eri"})
        assert companies.json()["data"] == [
            {"id": "ericsson", "label": "Ericsson", "tdoc_count": 2}
        ]
        filtered = await client.get(
            "/api/working-groups/RAN2/graph", params={"company_ids": "ericsson"}
        )
        assert filtered.json()["data"]["counts"]["tdocs"] == 2
        assert (await client.get("/api/working-groups/missing/graph")).status_code == 404
