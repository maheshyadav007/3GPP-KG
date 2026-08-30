from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from .constants import Conclusion, MatchMode
from .domain import SearchFilters, SearchRequest, TemporalScope
from .service import KnowledgeService


def temporal_scope(
    *,
    meeting_ids: list[str] | None = None,
    last_k_meetings: int | None = None,
    duration_months: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    default_last_k: int | None = None,
) -> TemporalScope:
    if not any((meeting_ids, last_k_meetings, duration_months, date_from, date_to)):
        last_k_meetings = default_last_k
    scope = TemporalScope(
        meeting_ids=meeting_ids,
        last_k_meetings=last_k_meetings,
        duration_months=duration_months,
        date_from=date_from,
        date_to=date_to,
    )
    selectors = sum(
        (
            bool(scope.meeting_ids),
            scope.last_k_meetings is not None,
            scope.duration_months is not None,
            scope.date_from is not None or scope.date_to is not None,
        )
    )
    if selectors != 1:
        raise ValueError("exactly one temporal selector is required")
    return scope


def create_mcp_server(service: KnowledgeService) -> FastMCP:
    mcp = FastMCP(
        "3GPP Evidence Graph",
        instructions=(
            "Use exact metadata tools before semantic synthesis. Treat evidence authority and "
            "completeness fields as part of every answer."
        ),
        stateless_http=True,
        json_response=True,
    )

    async def topic_search(
        query: str,
        working_groups: list[str] | None,
        scope: TemporalScope,
        top_k: int,
    ) -> dict[str, Any]:
        return (
            await service.search_tdocs(
                SearchRequest(
                    query=query,
                    filters=SearchFilters(
                        working_groups=working_groups or [],
                        topics=[query],
                        temporal=scope,
                    ),
                    top_k=top_k,
                )
            )
        ).model_dump(mode="json")

    @mcp.tool()
    async def list_meetings(
        working_groups: list[str],
        last_k_meetings: int | None = None,
        duration_months: int | None = None,
        meeting_ids: list[str] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        """List meetings using exactly one temporal selector; defaults to the latest three."""
        request = SearchRequest(
            filters=SearchFilters(
                temporal=temporal_scope(
                    last_k_meetings=last_k_meetings,
                    duration_months=duration_months,
                    meeting_ids=meeting_ids,
                    date_from=date_from,
                    date_to=date_to,
                    default_last_k=3,
                )
            )
        )
        return (await service.list_meetings(working_groups, request)).model_dump(mode="json")

    @mcp.tool()
    async def search_tdocs(
        query: str = "",
        working_groups: list[str] | None = None,
        companies: list[str] | None = None,
        releases: list[str] | None = None,
        statuses: list[str] | None = None,
        match_mode: str = "all",
        last_k_meetings: int | None = None,
        duration_months: int | None = None,
        meeting_ids: list[str] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        top_k: int = 10,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search TDocs with exact metadata and textual constraints."""
        request = SearchRequest(
            query=query,
            filters=SearchFilters(
                working_groups=working_groups or [],
                companies=companies or [],
                releases=releases or [],
                statuses=[Conclusion(value) for value in statuses or []],
                match_mode=MatchMode(match_mode),
                temporal=temporal_scope(
                    last_k_meetings=last_k_meetings,
                    duration_months=duration_months,
                    meeting_ids=meeting_ids,
                    date_from=date_from,
                    date_to=date_to,
                    default_last_k=3,
                ),
            ),
            top_k=top_k,
            cursor=cursor,
        )
        return (await service.search_tdocs(request)).model_dump(mode="json")

    @mcp.tool()
    async def get_tdoc(
        tdoc_id: str,
        block_limit: int = 200,
        cursor: str | None = None,
        start_block: int | None = None,
    ) -> dict[str, Any]:
        """Get one TDoc with its source-qualified evidence."""
        if block_limit < 1 or block_limit > 2000:
            raise ValueError("block_limit must be between 1 and 2000")
        return (
            await service.get_tdoc_detail(
                tdoc_id,
                block_limit=block_limit,
                cursor=cursor,
                start_block=start_block,
            )
        ).model_dump(mode="json")

    @mcp.tool()
    async def get_tdoc_sections(
        tdoc_id: str,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List the deterministic section tree and stable block anchors for a TDoc."""
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        return (
            await service.document_section_tree(tdoc_id, limit=limit, cursor=cursor)
        ).model_dump(mode="json")

    @mcp.tool()
    async def get_revision_chain(tdoc_id: str) -> dict[str, Any]:
        """Return the ordered revision chain containing a TDoc."""
        return (await service.revision_chain(tdoc_id)).model_dump(mode="json")

    @mcp.tool()
    async def get_relevant_passages(
        query: str,
        tdoc_ids: list[str] | None = None,
        meeting_ids: list[str] | None = None,
        last_k_meetings: int | None = None,
        duration_months: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        """Return evidence-bearing TDoc passages for focused comparison."""
        selected_meeting_ids: list[str] = []
        if not tdoc_ids or any((meeting_ids, last_k_meetings, duration_months, date_from, date_to)):
            scope = temporal_scope(
                meeting_ids=meeting_ids,
                last_k_meetings=last_k_meetings,
                duration_months=duration_months,
                date_from=date_from,
                date_to=date_to,
                default_last_k=3,
            )
            selected_meetings, _ = await service.repository.list_meetings(
                [], SearchRequest(filters=SearchFilters(temporal=scope), top_k=100)
            )
            selected_meeting_ids = [meeting.id for meeting in selected_meetings]
        result = await service.relevant_passages(
            query,
            tdoc_ids=tdoc_ids,
            meeting_ids=selected_meeting_ids,
            top_k=top_k,
        )
        return result.model_dump(mode="json")

    @mcp.tool()
    async def get_meeting_decisions(
        meeting_id: str,
        statuses: list[str] | None = None,
        top_k: int = 100,
    ) -> dict[str, Any]:
        """List decisions for one meeting without conflating conclusion statuses."""
        wanted = statuses or [
            "agreed",
            "approved",
            "rejected",
            "not_pursued",
            "postponed",
            "revised",
        ]
        return (
            await service.search_tdocs(
                SearchRequest(
                    filters=SearchFilters(
                        statuses=[Conclusion(value) for value in wanted],
                        temporal=TemporalScope(meeting_ids=[meeting_id]),
                    ),
                    top_k=top_k,
                )
            )
        ).model_dump(mode="json")

    @mcp.tool()
    async def get_meeting_brief(meeting_id: str) -> dict[str, Any]:
        """Build a deterministic, evidence-linked meeting briefing packet."""
        return (await service.newsletter_packet(meeting_id)).model_dump(mode="json")

    @mcp.tool()
    async def get_newsletter(meeting_id: str, edition: str = "provisional") -> dict[str, Any]:
        """Get the deterministic newsletter packet for a meeting edition."""
        return (await service.newsletter_packet(meeting_id, edition)).model_dump(mode="json")

    @mcp.tool()
    async def search_topics(
        query: str,
        working_groups: list[str] | None = None,
        meeting_ids: list[str] | None = None,
        last_k_meetings: int | None = None,
        duration_months: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        top_k: int = 20,
    ) -> dict[str, Any]:
        """Find canonical topic evidence through TDoc and agenda metadata."""
        return await topic_search(
            query,
            working_groups,
            temporal_scope(
                meeting_ids=meeting_ids,
                last_k_meetings=last_k_meetings,
                duration_months=duration_months,
                date_from=date_from,
                date_to=date_to,
                default_last_k=3,
            ),
            top_k,
        )

    @mcp.tool()
    async def trace_topic(
        topic: str,
        working_groups: list[str] | None = None,
        meeting_ids: list[str] | None = None,
        last_k_meetings: int | None = None,
        duration_months: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        top_k: int = 100,
    ) -> dict[str, Any]:
        """Trace topic-related TDocs across meetings in chronological source context."""
        return await topic_search(
            topic,
            working_groups,
            temporal_scope(
                meeting_ids=meeting_ids,
                last_k_meetings=last_k_meetings,
                duration_months=duration_months,
                date_from=date_from,
                date_to=date_to,
                default_last_k=3,
            ),
            top_k,
        )

    @mcp.tool()
    async def get_hot_topics(meeting_id: str) -> dict[str, Any]:
        """Rank meeting topics using transparent TDoc counts."""
        packet = await service.newsletter_packet(meeting_id)
        result = packet.model_dump(mode="json")
        result["data"] = packet.data.hot_topics if packet.data else []
        return result

    @mcp.tool()
    async def get_company_activity(meeting_id: str) -> dict[str, Any]:
        """Report company source activity without inferring unstated positions."""
        packet = await service.newsletter_packet(meeting_id)
        result = packet.model_dump(mode="json")
        result["data"] = packet.data.company_activity if packet.data else []
        return result

    @mcp.tool()
    async def get_spec_changes(
        specification: str,
        working_groups: list[str] | None = None,
        meeting_ids: list[str] | None = None,
        last_k_meetings: int | None = None,
        duration_months: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        top_k: int = 100,
    ) -> dict[str, Any]:
        """Find TDocs affecting a specification."""
        scope = temporal_scope(
            meeting_ids=meeting_ids,
            last_k_meetings=last_k_meetings,
            duration_months=duration_months,
            date_from=date_from,
            date_to=date_to,
            default_last_k=3,
        )
        return (
            await service.search_tdocs(
                SearchRequest(
                    filters=SearchFilters(
                        working_groups=working_groups or [],
                        specifications=[specification],
                        temporal=scope,
                    ),
                    top_k=top_k,
                )
            )
        ).model_dump(mode="json")

    @mcp.tool()
    async def compare_meetings(meeting_ids: list[str]) -> dict[str, Any]:
        """Return deterministic briefing packets for two or more meetings."""
        if len(meeting_ids) < 2:
            raise ValueError("compare_meetings requires at least two meeting IDs")
        packets = [await service.newsletter_packet(meeting_id) for meeting_id in meeting_ids]
        version = await service.repository.active_dataset_version()
        return {
            "data": [
                packet.data.model_dump(mode="json") if packet.data else None for packet in packets
            ],
            "evidence": [
                item.model_dump(mode="json") for packet in packets for item in packet.evidence
            ],
            "dataset_version": version,
            "completeness": "complete"
            if all(packet.completeness == "complete" for packet in packets)
            else "partial",
            "confidence": 1.0,
            "warnings": [warning for packet in packets for warning in packet.warnings],
            "next_cursor": None,
        }

    return mcp
