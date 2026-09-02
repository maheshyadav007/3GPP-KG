from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from .config import FeatureConfig, NewsletterConfig
from .constants import (
    NEWSLETTER_PROMPT_VERSION,
    MatchMode,
    NewsletterStatus,
)
from .domain import (
    Envelope,
    Meeting,
    NewsletterPacket,
    NewsletterRecord,
    Passage,
    RetrievalMetadata,
    SearchFilters,
    SearchRequest,
    TDoc,
    TDocDetail,
)
from .graph_view import FacetKind, build_graph, build_scope_graph, facet_options, filter_tdocs
from .models.base import EmbeddingClient
from .models.client import ModelEndpointError, OpenAICompatibleClient
from .newsletter import NewsletterPacketBuilder, NewsletterRenderer
from .repository import InMemoryRepository, Repository, decode_cursor, encode_cursor


class KnowledgeService:
    def __init__(
        self,
        repository: Repository,
        embedding_client: EmbeddingClient | None = None,
        rerank_client: OpenAICompatibleClient | None = None,
        newsletter_config: NewsletterConfig | None = None,
        generation_client: OpenAICompatibleClient | None = None,
        features: FeatureConfig | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_client = embedding_client
        self.rerank_client = rerank_client
        self.newsletter_config = newsletter_config or NewsletterConfig()
        self.newsletter_builder = NewsletterPacketBuilder(self.newsletter_config)
        self.generation_client = generation_client
        self.features = features or FeatureConfig()
        self.newsletter_renderer = NewsletterRenderer(
            self.features, generation_client, self.newsletter_config
        )

    async def list_meetings(
        self,
        working_groups: list[str],
        request: SearchRequest,
    ) -> Envelope[list[dict[str, Any]]]:
        meetings, cursor = await self.repository.list_meetings(working_groups, request)
        version = await self.repository.active_dataset_version()
        return Envelope(
            data=[meeting.model_dump(mode="json") for meeting in meetings],
            dataset_version=version,
            next_cursor=cursor,
        )

    async def meeting_summaries(
        self, working_groups: list[str], request: SearchRequest
    ) -> Envelope[list[dict[str, Any]]]:
        result = await self.list_meetings(working_groups, request)
        counts = await self.repository.meeting_tdoc_counts([item["id"] for item in result.data])
        return Envelope(
            data=[{**item, "tdoc_count": counts.get(item["id"], 0)} for item in result.data],
            dataset_version=result.dataset_version,
            next_cursor=result.next_cursor,
        )

    async def meeting_graph(
        self,
        meeting_id: str,
        *,
        query: str = "",
        company_ids: list[str] | None = None,
        topic_ids: list[str] | None = None,
        specification_ids: list[str] | None = None,
        match_mode: MatchMode = MatchMode.ALL,
    ) -> Envelope[dict[str, Any] | None]:
        meeting = await self.repository.meeting(meeting_id)
        version = await self.repository.active_dataset_version()
        if meeting is None:
            return Envelope(
                data=None,
                dataset_version=version,
                completeness="unavailable",
                confidence=0.0,
                warnings=[f"Meeting {meeting_id} was not found"],
            )
        all_tdocs = await self.repository.meeting_tdocs(meeting.id)
        selected = filter_tdocs(
            all_tdocs,
            query=query,
            company_ids=company_ids or [],
            topic_ids=topic_ids or [],
            specification_ids=specification_ids or [],
            match_mode=match_mode,
        )
        full_graph = build_graph(meeting, all_tdocs)
        graph = full_graph if len(selected) == len(all_tdocs) else build_graph(meeting, selected)
        return await self._graph_envelope(
            meetings=[meeting],
            all_tdocs=all_tdocs,
            selected=selected,
            graph=graph,
            full_graph=full_graph,
            scope_type="meeting",
            scope_id=meeting.id,
            scope_label=meeting.name,
            version=version,
            match_mode=match_mode,
            meeting=meeting,
        )

    async def working_group_graph(
        self,
        working_group_id: str,
        *,
        query: str = "",
        company_ids: list[str] | None = None,
        topic_ids: list[str] | None = None,
        specification_ids: list[str] | None = None,
        match_mode: MatchMode = MatchMode.ALL,
    ) -> Envelope[dict[str, Any] | None]:
        working_group_id = working_group_id.upper()
        meetings, all_tdocs = await self._working_group_scope(working_group_id)
        version = await self.repository.active_dataset_version()
        if not meetings:
            return Envelope(
                data=None,
                dataset_version=version,
                completeness="unavailable",
                confidence=0.0,
                warnings=[f"Working group {working_group_id} was not found"],
            )
        selected = filter_tdocs(
            all_tdocs,
            query=query,
            company_ids=company_ids or [],
            topic_ids=topic_ids or [],
            specification_ids=specification_ids or [],
            match_mode=match_mode,
        )
        full_graph = build_scope_graph(meetings, all_tdocs)
        graph = (
            full_graph if len(selected) == len(all_tdocs) else build_scope_graph(meetings, selected)
        )
        return await self._graph_envelope(
            meetings=meetings,
            all_tdocs=all_tdocs,
            selected=selected,
            graph=graph,
            full_graph=full_graph,
            scope_type="working_group",
            scope_id=working_group_id,
            scope_label=working_group_id,
            version=version,
            match_mode=match_mode,
        )

    async def meeting_facets(
        self, meeting_id: str, facet: FacetKind, query: str, limit: int
    ) -> Envelope[list[dict[str, Any]]] | None:
        meeting = await self.repository.meeting(meeting_id)
        if meeting is None:
            return None
        values = facet_options(await self.repository.meeting_tdocs(meeting.id), facet, query, limit)
        return Envelope(
            data=values,
            dataset_version=await self.repository.active_dataset_version(),
            completeness="complete",
            confidence=1.0,
        )

    async def working_group_facets(
        self, working_group_id: str, facet: FacetKind, query: str, limit: int
    ) -> Envelope[list[dict[str, Any]]] | None:
        meetings, tdocs = await self._working_group_scope(working_group_id.upper())
        if not meetings:
            return None
        return Envelope(
            data=facet_options(tdocs, facet, query, limit),
            dataset_version=await self.repository.active_dataset_version(),
            completeness="complete",
            confidence=1.0,
        )

    async def _working_group_scope(self, working_group_id: str) -> tuple[list[Meeting], list[TDoc]]:
        meetings: list[Meeting] = []
        cursor: str | None = None
        while True:
            page, cursor = await self.repository.list_meetings(
                [working_group_id],
                SearchRequest(filters=SearchFilters(), top_k=100, cursor=cursor),
            )
            meetings.extend(page)
            if cursor is None:
                break
        pages: list[list[TDoc]] = []
        for offset in range(0, len(meetings), 20):
            pages.extend(
                await asyncio.gather(
                    *(
                        self.repository.meeting_tdocs(meeting.id)
                        for meeting in meetings[offset : offset + 20]
                    )
                )
            )
        return meetings, sorted((tdoc for page in pages for tdoc in page), key=lambda item: item.id)

    async def _graph_envelope(
        self,
        *,
        meetings: list[Meeting],
        all_tdocs: list[TDoc],
        selected: list[TDoc],
        graph: dict[str, Any],
        full_graph: dict[str, Any],
        scope_type: str,
        scope_id: str,
        scope_label: str,
        version: str,
        match_mode: MatchMode,
        meeting: Meeting | None = None,
    ) -> Envelope[dict[str, Any] | None]:
        evidence_ids = list(
            dict.fromkeys(identifier for tdoc in selected for identifier in tdoc.evidence_ids)
        )
        data: dict[str, Any] = {
            "scope": {"type": scope_type, "id": scope_id, "label": scope_label},
            "meetings": [item.model_dump(mode="json") for item in meetings],
            "nodes": graph["nodes"],
            "edges": graph["edges"],
            "counts": {
                "meetings": len(meetings),
                "tdocs": len(selected),
                "nodes": len(graph["nodes"]),
                "edges": len(graph["edges"]),
                "total_tdocs": len(all_tdocs),
                "total_nodes": len(full_graph["nodes"]),
                "total_edges": len(full_graph["edges"]),
            },
            "revision_stats": graph["revision_stats"],
            "total_revision_stats": full_graph["revision_stats"],
            "match_mode": match_mode.value,
        }
        if meeting:
            data["meeting"] = meeting.model_dump(mode="json")
        return Envelope(
            data=data,
            evidence=await self.repository.evidence(evidence_ids),
            dataset_version=version,
            completeness="complete",
            confidence=1.0,
        )

    async def search_tdocs(self, request: SearchRequest) -> Envelope[list[TDoc]]:
        tdocs, cursor = await self.repository.search_tdocs(request)
        evidence_ids = list(dict.fromkeys(eid for tdoc in tdocs for eid in tdoc.evidence_ids))
        evidence = await self.repository.evidence(evidence_ids)
        version = await self.repository.active_dataset_version()
        return Envelope(data=tdocs, evidence=evidence, dataset_version=version, next_cursor=cursor)

    async def get_tdoc(self, tdoc_id: str) -> Envelope[TDoc | None]:
        tdoc = await self.repository.get_tdoc(tdoc_id)
        evidence = await self.repository.evidence(tdoc.evidence_ids if tdoc else [])
        version = await self.repository.active_dataset_version()
        return Envelope(
            data=tdoc,
            evidence=evidence,
            dataset_version=version,
            completeness="complete" if tdoc else "unavailable",
            confidence=1.0 if tdoc else 0.0,
            warnings=[] if tdoc else [f"TDoc {tdoc_id} was not found"],
        )

    async def get_tdoc_detail(
        self,
        tdoc_id: str,
        *,
        block_limit: int = 500,
        cursor: str | None = None,
        start_block: int | None = None,
    ) -> Envelope[TDocDetail | None]:
        base = await self.get_tdoc(tdoc_id)
        if base.data is None:
            return Envelope(
                data=None,
                evidence=base.evidence,
                dataset_version=base.dataset_version,
                completeness=base.completeness,
                confidence=base.confidence,
                warnings=base.warnings,
            )
        if cursor and start_block is not None:
            raise ValueError("cursor and start_block are mutually exclusive")
        offset = start_block if start_block is not None else decode_cursor(cursor)
        blocks = await self.repository.document_blocks(
            base.data.id,
            offset=offset,
            limit=block_limit + 1,
        )
        has_more = len(blocks) > block_limit
        blocks = blocks[:block_limit]
        return Envelope(
            data=TDocDetail(tdoc=base.data, blocks=blocks),
            evidence=base.evidence,
            dataset_version=base.dataset_version,
            completeness="partial" if has_more or not blocks else "complete",
            confidence=base.confidence,
            warnings=(
                ["Additional document blocks are available"]
                if has_more
                else ([] if blocks else ["TDoc body has not been ingested"])
            ),
            next_cursor=encode_cursor(offset + block_limit) if has_more else None,
        )

    async def document_section_tree(
        self,
        tdoc_id: str,
        *,
        limit: int = 200,
        cursor: str | None = None,
    ) -> Envelope[list[dict[str, Any]]]:
        base = await self.get_tdoc(tdoc_id)
        if base.data is None:
            return Envelope(
                data=[],
                evidence=base.evidence,
                dataset_version=base.dataset_version,
                completeness="unavailable",
                confidence=0.0,
                warnings=base.warnings,
            )
        offset = decode_cursor(cursor)
        section_tree = await self.repository.document_section_tree(base.data.id)
        page = section_tree[offset : offset + limit]
        has_more = offset + limit < len(section_tree)
        return Envelope(
            data=[entry.model_dump(mode="json") for entry in page],
            evidence=base.evidence,
            dataset_version=base.dataset_version,
            completeness="partial" if has_more else ("complete" if page else "unavailable"),
            confidence=base.confidence if page else 0.0,
            warnings=[] if page else ["TDoc section tree is unavailable"],
            next_cursor=encode_cursor(offset + limit) if has_more else None,
        )

    async def relevant_passages(
        self,
        query: str,
        *,
        tdoc_ids: list[str] | None = None,
        meeting_ids: list[str] | None = None,
        top_k: int = 10,
        query_embedding: list[float] | None = None,
    ) -> Envelope[list[Passage]]:
        warnings: list[str] = []
        active_profile = await self.repository.active_embedding_profile()
        retrieval_mode = "lexical"
        if query_embedding is None and self.embedding_client and query.strip():
            if active_profile is None:
                warnings.append("No active embedding profile; lexical retrieval was used")
                retrieval_mode = "lexical_fallback"
            elif active_profile.id != self.embedding_client.profile_id:
                warnings.append(
                    "Configured model does not match the active embedding profile; "
                    "lexical retrieval was used"
                )
                retrieval_mode = "lexical_fallback"
            elif not self.embedding_client.available():
                warnings.append("Cached ONNX model is unavailable; lexical retrieval was used")
                retrieval_mode = "lexical_fallback"
            else:
                try:
                    query_embedding = (await self.embedding_client.embed_queries([query]))[0]
                    retrieval_mode = "hybrid"
                except (ModelEndpointError, OSError, TimeoutError):
                    warnings.append("Semantic retrieval failed; lexical retrieval was used")
                    retrieval_mode = "lexical_fallback"
        elif query_embedding is not None:
            if active_profile is not None and len(query_embedding) == active_profile.dimensions:
                retrieval_mode = "hybrid"
            else:
                query_embedding = None
                retrieval_mode = "lexical_fallback"
                warnings.append(
                    "Query embedding does not match an active profile; lexical retrieval was used"
                )
        passages = await self.repository.search_passages(
            query,
            tdoc_ids=tdoc_ids or [],
            meeting_ids=meeting_ids or [],
            top_k=top_k,
            query_embedding=query_embedding,
            embedding_profile_id=(
                self.embedding_client.profile_id
                if query_embedding is not None and self.embedding_client
                else (active_profile.id if query_embedding is not None and active_profile else None)
            ),
        )
        if self.rerank_client and passages:
            try:
                ranked = await self.rerank_client.rerank(
                    query, [passage.text for passage in passages]
                )
                passages = [passages[index] for index, _ in ranked]
            except (ModelEndpointError, OSError, TimeoutError):
                warnings.append("Reranking failed; hybrid order was retained")
        evidence_ids = list(
            dict.fromkeys(identifier for passage in passages for identifier in passage.evidence_ids)
        )
        evidence = await self.repository.evidence(evidence_ids)
        version = await self.repository.active_dataset_version()
        return Envelope(
            data=passages,
            evidence=evidence,
            dataset_version=version,
            completeness="complete" if passages else "unavailable",
            confidence=max((passage.score for passage in passages), default=0.0),
            warnings=warnings
            if passages
            else [*warnings, "No evidence-bearing passages matched the query"],
            retrieval=RetrievalMetadata(
                mode=retrieval_mode,
                embedding_profile=active_profile if retrieval_mode == "hybrid" else None,
            ),
        )

    async def warmup_models(self) -> None:
        if self.embedding_client and self.embedding_client.available():
            try:
                await self.embedding_client.warmup()
            except (ModelEndpointError, OSError, TimeoutError):
                return

    async def close_models(self) -> None:
        if self.embedding_client:
            await self.embedding_client.close()
        if self.rerank_client and self.rerank_client is not self.embedding_client:
            await self.rerank_client.close()
        if self.generation_client and self.generation_client not in {
            self.embedding_client,
            self.rerank_client,
        }:
            await self.generation_client.close()

    async def semantic_health(self) -> dict[str, Any]:
        database_status = await self.repository.embedding_status()
        configured = self.embedding_client is not None
        cached = bool(self.embedding_client and self.embedding_client.available())
        profile_matches = bool(
            self.embedding_client
            and database_status["profile"]
            and database_status["profile"]["id"] == self.embedding_client.profile_id
        )
        return {
            **database_status,
            "configured": configured,
            "model_cached": cached,
            "profile_matches_config": profile_matches,
            "ready": bool(database_status["active"] and cached and profile_matches),
        }

    async def revision_chain(self, tdoc_id: str) -> Envelope[list[str]]:
        if isinstance(self.repository, InMemoryRepository):
            chain = self.repository.revision_chain(tdoc_id)
        else:
            chain = await self._revision_chain_generic(tdoc_id)
        chain_tdocs = [await self.repository.get_tdoc(identifier) for identifier in chain]
        evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for tdoc in chain_tdocs
                if tdoc is not None
                for evidence_id in tdoc.evidence_ids
            )
        )
        evidence = await self.repository.evidence(evidence_ids)
        version = await self.repository.active_dataset_version()
        return Envelope(
            data=chain,
            evidence=evidence,
            dataset_version=version,
            completeness="complete" if chain else "unavailable",
            confidence=1.0 if chain else 0.0,
            warnings=[] if evidence or not chain else ["Revision chain has no source evidence"],
        )

    async def _revision_chain_generic(self, tdoc_id: str) -> list[str]:
        current = await self.repository.get_tdoc(tdoc_id)
        if not current:
            return []
        seen: set[str] = set()
        while current.revised_from and current.id not in seen:
            seen.add(current.id)
            previous = await self.repository.get_tdoc(current.revised_from)
            if not previous:
                break
            current = previous
        chain: list[str] = []
        seen.clear()
        while current.id not in seen:
            seen.add(current.id)
            chain.append(current.id)
            if not current.revised_to:
                break
            following = await self.repository.get_tdoc(current.revised_to)
            if not following:
                break
            current = following
        return chain

    async def newsletter_packet(
        self, meeting_id: str, edition: str = "provisional"
    ) -> Envelope[NewsletterPacket | None]:
        if edition not in {"provisional", "final"}:
            raise ValueError("edition must be provisional or final")
        version = await self.repository.active_dataset_version()
        meeting = await self.repository.meeting(meeting_id)
        if meeting is None:
            return Envelope(
                data=None,
                dataset_version=version,
                completeness="unavailable",
                confidence=0,
                warnings=[f"Meeting {meeting_id} was not found"],
            )
        wg_meetings, _ = await self.repository.list_meetings(
            [meeting.working_group_id],
            SearchRequest(
                top_k=100,
            ),
        )
        current_index = next(
            (index for index, item in enumerate(wg_meetings) if item.id == meeting.id), None
        )
        if current_index is None:
            comparison_meetings = [meeting]
        else:
            older = wg_meetings[
                current_index + 1 : current_index + 1 + self.newsletter_config.last_k_meetings
            ]
            comparison_meetings = [*reversed(older), meeting]
        tdocs_by_meeting = {
            item.id: await self.repository.meeting_tdocs(item.id) for item in comparison_meetings
        }
        all_evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for selected in tdocs_by_meeting.values()
                for tdoc in selected
                for evidence_id in tdoc.evidence_ids
            )
        )
        evidence = await self.repository.evidence(all_evidence_ids)
        packet = self.newsletter_builder.build(
            meeting=meeting,
            dataset_version=version,
            comparison_meetings=comparison_meetings,
            tdocs_by_meeting=tdocs_by_meeting,
            evidence=evidence,
            edition="final" if edition == "final" else "provisional",
            provisional_packet=(
                provisional.packet
                if edition == "final"
                and (
                    provisional := await self.repository.latest_newsletter(
                        meeting.id, "provisional", across_datasets=True
                    )
                )
                else None
            ),
        )
        current_tdocs = tdocs_by_meeting[meeting.id]
        resolved_evidence = {item.id for item in evidence if (item.excerpt or "").strip()}
        missing_evidence = [
            tdoc.id
            for tdoc in current_tdocs
            if not tdoc.evidence_ids or not set(tdoc.evidence_ids) & resolved_evidence
        ]
        warnings: list[str] = []
        if edition == "final" and meeting.readiness != "final_ready":
            warnings.append("Final report evidence is not yet available")
        if missing_evidence:
            warnings.append(
                f"{len(missing_evidence)} TDocs do not have evidence and cannot be published"
            )
        coverage = (
            (len(current_tdocs) - len(missing_evidence)) / len(current_tdocs)
            if current_tdocs
            else 0
        )
        if coverage < self.newsletter_config.minimum_evidence_coverage:
            warnings.append(
                f"Evidence coverage {coverage:.3f} is below the configured newsletter threshold"
            )
        complete = bool(current_tdocs) and not warnings
        return Envelope(
            data=packet,
            evidence=evidence,
            dataset_version=version,
            completeness="complete" if complete else "partial",
            confidence=coverage,
            warnings=warnings,
        )

    async def build_newsletter(
        self,
        meeting_id: str,
        edition: str = "provisional",
        *,
        render: bool = False,
    ) -> Envelope[NewsletterRecord | None]:
        packet_envelope = await self.newsletter_packet(meeting_id, edition)
        if packet_envelope.data is None:
            return Envelope(
                data=None,
                evidence=packet_envelope.evidence,
                dataset_version=packet_envelope.dataset_version,
                completeness=packet_envelope.completeness,
                confidence=packet_envelope.confidence,
                warnings=packet_envelope.warnings,
            )
        packet = packet_envelope.data
        packet_sha = self._newsletter_hash(
            packet.model_dump(mode="json"), transient={"generated_at"}
        )
        record = NewsletterRecord(
            id=packet.id,
            dataset_version=packet_envelope.dataset_version,
            meeting_id=meeting_id,
            edition=packet.edition,
            packet=packet,
            status=NewsletterStatus.PACKET_READY,
            packet_sha256=packet_sha,
            created_at=packet.generated_at,
        )
        record = await self.repository.save_newsletter(record)
        if not render:
            return Envelope(
                data=record,
                evidence=packet_envelope.evidence,
                dataset_version=packet_envelope.dataset_version,
                completeness=packet_envelope.completeness,
                confidence=packet_envelope.confidence,
                warnings=packet_envelope.warnings,
            )
        try:
            rendered = await self.newsletter_renderer.render(packet_envelope)
        except (ModelEndpointError, ValueError, OSError, TimeoutError) as exc:
            failed = record.model_copy(
                update={
                    "status": NewsletterStatus.GENERATION_FAILED,
                    "generation_error": str(exc),
                }
            )
            await self.repository.save_newsletter(failed)
            raise
        if rendered.data is None:
            return Envelope(
                data=record,
                evidence=packet_envelope.evidence,
                dataset_version=packet_envelope.dataset_version,
                completeness="partial",
                confidence=packet_envelope.confidence,
                warnings=[*packet_envelope.warnings, *rendered.warnings],
            )
        rendered_data = rendered.data.model_dump(mode="json")
        status = (
            NewsletterStatus.PENDING_APPROVAL
            if self.newsletter_config.require_human_approval
            else NewsletterStatus.APPROVED
        )
        generated_record = record.model_copy(
            update={
                "rendered": rendered_data,
                "status": status,
                "rendered_sha256": self._newsletter_hash(rendered_data),
                "model": self.generation_client.model_name if self.generation_client else None,
                "model_revision": (
                    self.generation_client.revision if self.generation_client else None
                ),
                "prompt_version": NEWSLETTER_PROMPT_VERSION,
                "generation_error": None,
            }
        )
        generated_record = await self.repository.save_newsletter(generated_record)
        return Envelope(
            data=generated_record,
            evidence=packet_envelope.evidence,
            dataset_version=packet_envelope.dataset_version,
            confidence=packet_envelope.confidence,
            warnings=packet_envelope.warnings,
        )

    async def get_newsletter_record(
        self,
        meeting_id: str,
        edition: str = "provisional",
        *,
        approved_only: bool = False,
    ) -> Envelope[NewsletterRecord | None]:
        if edition not in {"provisional", "final"}:
            raise ValueError("edition must be provisional or final")
        version = await self.repository.active_dataset_version()
        record = await self.repository.latest_newsletter(
            meeting_id, edition, approved_only=approved_only
        )
        evidence = await self.repository.evidence(record.packet.evidence_ids) if record else []
        return Envelope(
            data=record,
            evidence=evidence,
            dataset_version=version,
            completeness="complete" if record else "unavailable",
            confidence=1 if record else 0,
            warnings=[] if record else [f"No {edition} newsletter exists for {meeting_id}"],
        )

    async def review_newsletter(
        self,
        newsletter_id: str,
        decision: str,
        reviewer: str,
        notes: str = "",
    ) -> Envelope[NewsletterRecord | None]:
        if not reviewer.strip():
            raise ValueError("reviewer is required")
        try:
            status = NewsletterStatus(decision)
        except ValueError as exc:
            raise ValueError("decision must be approved or rejected") from exc
        if status not in {NewsletterStatus.APPROVED, NewsletterStatus.REJECTED}:
            raise ValueError("decision must be approved or rejected")
        record = await self.repository.review_newsletter(
            newsletter_id, status, reviewer.strip(), notes.strip()
        )
        version = await self.repository.active_dataset_version()
        evidence = await self.repository.evidence(record.packet.evidence_ids) if record else []
        return Envelope(
            data=record,
            evidence=evidence,
            dataset_version=version,
            completeness="complete" if record else "unavailable",
            confidence=1 if record else 0,
            warnings=[] if record else [f"Newsletter {newsletter_id} was not found"],
        )

    @staticmethod
    def _newsletter_hash(value: dict[str, Any], transient: set[str] | None = None) -> str:
        canonical = {key: item for key, item in value.items() if key not in (transient or set())}
        serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()
