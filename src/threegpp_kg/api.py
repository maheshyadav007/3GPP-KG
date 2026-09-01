from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Mount

from .config import load_settings
from .constants import Conclusion, MatchMode
from .domain import SearchFilters, SearchRequest, TemporalScope
from .fixtures import demo_repository
from .mcp_server import create_mcp_server
from .models.client import create_model_client
from .repository import SqlRepository
from .security import OidcAuthMiddleware, OidcTokenValidator, TokenValidator
from .service import KnowledgeService
from .storage.database import create_engine_and_session


def create_app(
    service: KnowledgeService | None = None,
    token_validator: TokenValidator | None = None,
) -> FastAPI:
    settings = load_settings()
    engine = None
    if service is not None:
        knowledge = service
    elif settings.database.mode == "sql":
        engine, sessions = create_engine_and_session(settings.database)
        embedding_client = (
            create_model_client(
                settings.models.embedding,
                timeout_seconds=settings.models.timeout_seconds,
                expected_dimensions=settings.models.embedding.dimensions,
                retries=settings.models.retries,
            )
            if settings.models.embedding.base_url and settings.models.embedding.model
            else None
        )
        rerank_client = (
            create_model_client(
                settings.models.rerank,
                timeout_seconds=settings.models.timeout_seconds,
                retries=settings.models.retries,
            )
            if settings.models.rerank.base_url and settings.models.rerank.model
            else None
        )
        knowledge = KnowledgeService(
            SqlRepository(
                sessions,
                settings.retrieval,
                preview_dataset_version=settings.database.preview_dataset_version,
            ),
            embedding_client,
            rerank_client,
        )
    else:
        knowledge = KnowledgeService(demo_repository())
    mcp = create_mcp_server(knowledge)
    mcp.settings.streamable_http_path = "/"

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        async with mcp.session_manager.run():
            yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(
        title=settings.app.name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.security.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Mcp-Session-Id"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    if settings.security.auth_required:
        validator = token_validator or OidcTokenValidator(settings.security)
        app.add_middleware(OidcAuthMiddleware, validator=validator)

    def complete_graph_response(
        result: Any,
        *,
        missing_message: str,
        limit_name: str,
        max_nodes: int,
        max_edges: int,
    ) -> JSONResponse:
        if result.data is None:
            raise HTTPException(status_code=404, detail=missing_message)
        counts = result.data["counts"]
        if counts["nodes"] > max_nodes or counts["edges"] > max_edges:
            raise HTTPException(
                status_code=413,
                detail={
                    "message": (
                        f"complete {limit_name} graph exceeds configured safety limits"
                    ),
                    "counts": counts,
                    "max_nodes": max_nodes,
                    "max_edges": max_edges,
                },
            )
        if settings.database.preview_dataset_version:
            result.warnings.append(
                "Preview dataset is inactive; graph contents are complete for this dataset version."
            )
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "dataset_version": await knowledge.repository.active_dataset_version(),
        }

    @app.post("/api/tdocs/search")
    async def search_tdocs(request: SearchRequest) -> JSONResponse:
        result = await knowledge.search_tdocs(request)
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/api/tdocs/{tdoc_id}")
    async def get_tdoc(
        tdoc_id: str,
        block_limit: int = Query(default=settings.graph.document_block_page_size, ge=1),
        cursor: str | None = None,
        start_block: int | None = Query(default=None, ge=0),
    ) -> JSONResponse:
        if block_limit > settings.graph.max_document_block_page_size:
            raise HTTPException(status_code=422, detail="document block limit exceeds maximum")
        result = await knowledge.get_tdoc_detail(
            tdoc_id,
            block_limit=block_limit,
            cursor=cursor,
            start_block=start_block,
        )
        if result.data is None:
            raise HTTPException(status_code=404, detail=f"TDoc {tdoc_id} was not found")
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/api/tdocs/{tdoc_id}/sections")
    async def get_tdoc_sections(
        tdoc_id: str,
        limit: int = Query(default=settings.graph.document_section_page_size, ge=1),
        cursor: str | None = None,
    ) -> JSONResponse:
        if limit > settings.graph.max_document_section_page_size:
            raise HTTPException(status_code=422, detail="document section limit exceeds maximum")
        result = await knowledge.document_section_tree(tdoc_id, limit=limit, cursor=cursor)
        if result.completeness == "unavailable" and not result.data:
            raise HTTPException(status_code=404, detail=f"TDoc {tdoc_id} sections were not found")
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/api/meetings")
    async def list_meetings(
        working_group: Annotated[list[str] | None, Query()] = None,
        last_k_meetings: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> JSONResponse:
        result = await knowledge.meeting_summaries(
            working_group or [],
            SearchRequest(
                filters=SearchFilters(temporal=TemporalScope(last_k_meetings=last_k_meetings)),
                top_k=limit,
            ),
        )
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/api/meetings/{meeting_id}/graph")
    async def meeting_graph(
        meeting_id: str,
        query: str = "",
        company_ids: Annotated[list[str] | None, Query()] = None,
        topic_ids: Annotated[list[str] | None, Query()] = None,
        specification_ids: Annotated[list[str] | None, Query()] = None,
        match_mode: MatchMode = MatchMode.ALL,
    ) -> JSONResponse:
        result = await knowledge.meeting_graph(
            meeting_id,
            query=query,
            company_ids=company_ids,
            topic_ids=topic_ids,
            specification_ids=specification_ids,
            match_mode=match_mode,
        )
        return complete_graph_response(
            result,
            missing_message=f"Meeting {meeting_id} was not found",
            limit_name="meeting",
            max_nodes=settings.graph.max_meeting_nodes,
            max_edges=settings.graph.max_meeting_edges,
        )

    @app.get("/api/meetings/{meeting_id}/facets/{facet}")
    async def meeting_facets(
        meeting_id: str,
        facet: str,
        q: str = "",
        limit: int = Query(default=20, ge=1, le=100),
    ) -> JSONResponse:
        if facet not in {"company", "topic", "specification"}:
            raise HTTPException(status_code=422, detail="unsupported graph facet")
        result = await knowledge.meeting_facets(meeting_id, facet, q, limit)  # type: ignore[arg-type]
        if result is None:
            raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} was not found")
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/api/working-groups/{working_group_id}/graph")
    async def working_group_graph(
        working_group_id: str,
        query: str = "",
        company_ids: Annotated[list[str] | None, Query()] = None,
        topic_ids: Annotated[list[str] | None, Query()] = None,
        specification_ids: Annotated[list[str] | None, Query()] = None,
        match_mode: MatchMode = MatchMode.ALL,
    ) -> JSONResponse:
        result = await knowledge.working_group_graph(
            working_group_id,
            query=query,
            company_ids=company_ids,
            topic_ids=topic_ids,
            specification_ids=specification_ids,
            match_mode=match_mode,
        )
        return complete_graph_response(
            result,
            missing_message=f"Working group {working_group_id.upper()} was not found",
            limit_name="working group",
            max_nodes=settings.graph.max_working_group_nodes,
            max_edges=settings.graph.max_working_group_edges,
        )

    @app.get("/api/working-groups/{working_group_id}/facets/{facet}")
    async def working_group_facets(
        working_group_id: str,
        facet: str,
        q: str = "",
        limit: int = Query(default=20, ge=1, le=100),
    ) -> JSONResponse:
        if facet not in {"company", "topic", "specification"}:
            raise HTTPException(status_code=422, detail="unsupported graph facet")
        result = await knowledge.working_group_facets(
            working_group_id, facet, q, limit  # type: ignore[arg-type]
        )
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"Working group {working_group_id.upper()} was not found",
            )
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/api/graph")
    async def graph(
        query: str = "",
        working_groups: Annotated[list[str] | None, Query()] = None,
        companies: Annotated[list[str] | None, Query()] = None,
        topics: Annotated[list[str] | None, Query()] = None,
        releases: Annotated[list[str] | None, Query()] = None,
        specifications: Annotated[list[str] | None, Query()] = None,
        statuses: Annotated[list[Conclusion] | None, Query()] = None,
        match_mode: MatchMode = MatchMode.ALL,
        meeting_ids: Annotated[list[str] | None, Query()] = None,
        limit: int = Query(default=settings.graph.default_node_limit, ge=1),
    ) -> dict[str, Any]:
        if limit > settings.graph.max_node_limit:
            raise HTTPException(
                status_code=422, detail="graph node limit exceeds configured maximum"
            )
        filters = SearchFilters(
            working_groups=working_groups or [],
            companies=companies or [],
            topics=topics or [],
            releases=releases or [],
            specifications=specifications or [],
            statuses=statuses or [],
            match_mode=match_mode,
            temporal=TemporalScope(meeting_ids=meeting_ids),
        )
        balanced = not query.strip() and not any(
            (companies, topics, releases, specifications, statuses, meeting_ids)
        )
        if balanced:
            meetings = await knowledge.list_meetings(
                working_groups or [],
                SearchRequest(filters=SearchFilters(), top_k=100),
            )
            per_meeting = max(1, min(10, 100 // max(len(meetings.data), 1)))
            pages = await asyncio.gather(
                *(
                    knowledge.search_tdocs(
                        SearchRequest(
                            filters=SearchFilters(
                                temporal=TemporalScope(meeting_ids=[meeting["id"]])
                            ),
                            top_k=per_meeting,
                        )
                    )
                    for meeting in meetings.data
                )
            )
            tdocs = [tdoc for page in pages for tdoc in page.data]
            evidence = list(
                {
                    item.id: item
                    for page in pages
                    for item in page.evidence
                }.values()
            )
            dataset_version = meetings.dataset_version
            next_cursor = None
        else:
            result = await knowledge.search_tdocs(
                SearchRequest(query=query, filters=filters, top_k=min(limit, 100))
            )
            tdocs = result.data
            evidence = result.evidence
            dataset_version = result.dataset_version
            next_cursor = result.next_cursor
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []
        for tdoc in tdocs:
            nodes[tdoc.id] = {
                "id": tdoc.id,
                "type": "tdoc",
                "label": tdoc.id,
                "title": tdoc.title,
                "status": tdoc.status,
            }
            meeting_node_id = f"meeting:{tdoc.meeting_id}"
            nodes[meeting_node_id] = {
                "id": meeting_node_id,
                "type": "meeting",
                "label": tdoc.meeting_id,
            }
            edges.append({"source": meeting_node_id, "target": tdoc.id, "type": "contains"})
            for company in (part.strip() for part in tdoc.source.split(",") if part.strip()):
                company_id = f"company:{company.lower()}"
                nodes[company_id] = {"id": company_id, "type": "company", "label": company}
                edges.append({"source": company_id, "target": tdoc.id, "type": "submitted_by"})
            if tdoc.agenda_description:
                topic_id = f"topic:{tdoc.agenda_description.lower()}"
                nodes[topic_id] = {
                    "id": topic_id,
                    "type": "topic",
                    "label": tdoc.agenda_description,
                }
                edges.append({"source": tdoc.id, "target": topic_id, "type": "mentions_topic"})
            for specification in tdoc.specifications:
                specification_id = f"spec:{specification.lower()}"
                nodes[specification_id] = {
                    "id": specification_id,
                    "type": "specification",
                    "label": specification,
                }
                edges.append(
                    {"source": tdoc.id, "target": specification_id, "type": "affects_spec"}
                )
            for release in tdoc.releases:
                release_id = f"release:{release.lower()}"
                nodes[release_id] = {
                    "id": release_id,
                    "type": "release",
                    "label": release,
                }
                edges.append({"source": tdoc.id, "target": release_id, "type": "targets_release"})
            for work_item in tdoc.work_items:
                work_item_id = f"work-item:{work_item.lower()}"
                nodes[work_item_id] = {
                    "id": work_item_id,
                    "type": "work_item",
                    "label": work_item,
                }
                edges.append(
                    {
                        "source": tdoc.id,
                        "target": work_item_id,
                        "type": "related_to_work_item",
                    }
                )
            if tdoc.revised_from:
                edges.append({"source": tdoc.id, "target": tdoc.revised_from, "type": "revises"})
        selected_nodes = list(nodes.values())[:limit]
        selected_ids = {node["id"] for node in selected_nodes}
        selected_edges = [
            edge
            for edge in edges
            if edge["source"] in selected_ids and edge["target"] in selected_ids
        ][: settings.graph.max_edges]
        return {
            "data": {
                "nodes": selected_nodes,
                "edges": selected_edges,
                "match_mode": match_mode.value,
            },
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "dataset_version": dataset_version,
            "completeness": (
                "partial" if settings.database.preview_dataset_version else "complete"
            ),
            "confidence": 1.0,
            "warnings": (
                ["Preview dataset is still building; graph contents may change."]
                if settings.database.preview_dataset_version
                else []
            ),
            "next_cursor": next_cursor,
        }

    @app.get("/api/newsletters/{meeting_id}")
    async def newsletter(meeting_id: str, edition: str = "provisional") -> JSONResponse:
        result = await knowledge.newsletter_packet(meeting_id, edition)
        if result.data is None:
            raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} was not found")
        return JSONResponse(result.model_dump(mode="json"))

    app.router.routes.append(Mount("/mcp", app=mcp.streamable_http_app()))
    return app


app = create_app()
