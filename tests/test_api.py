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
        assert health.json() == {"status": "ok", "dataset_version": "test-v1"}
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
        assert {node["type"] for node in body["data"]["nodes"]} == {
            "tdoc",
            "company",
            "topic",
            "meeting",
            "specification",
            "release",
        }
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
            node["label"]
            for node in balanced.json()["data"]["nodes"]
            if node["type"] == "meeting"
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

        newsletter = await client.get("/api/newsletters/RAN2-133", params={"edition": "final"})
        assert newsletter.status_code == 200
        assert newsletter.json()["data"]["edition"] == "final"

        assert (await client.get("/api/tdocs/missing")).status_code == 404
        assert (await client.get("/api/newsletters/missing")).status_code == 404
