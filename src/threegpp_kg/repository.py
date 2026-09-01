from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any, Protocol, TypeVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import Text, and_, cast, desc, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from .config import RetrievalConfig
from .constants import AUTHORITY_RANK, BlockKind, Conclusion, EvidenceAuthority, MatchMode
from .domain import (
    DocumentBlock,
    DocumentSectionNode,
    EmbeddingProfileInfo,
    EvidenceRef,
    Meeting,
    Passage,
    RetrievalChunk,
    SearchFilters,
    SearchRequest,
    TDoc,
)
from .retrieval import rank_chunks, reciprocal_rank_fusion
from .storage.database import (
    ChunkEmbeddingRow,
    DatasetEmbeddingProfileRow,
    DatasetVersionRow,
    DocumentBlockRow,
    EmbeddingProfileRow,
    EvidenceRow,
    MeetingRow,
    RetrievalChunkRow,
    TDocRow,
)


class Repository(Protocol):
    async def active_dataset_version(self) -> str: ...

    async def active_embedding_profile(self) -> EmbeddingProfileInfo | None: ...

    async def embedding_status(self) -> dict[str, Any]: ...

    async def list_meetings(
        self, working_groups: list[str], request: SearchRequest
    ) -> tuple[list[Meeting], str | None]: ...

    async def search_tdocs(self, request: SearchRequest) -> tuple[list[TDoc], str | None]: ...

    async def meeting(self, meeting_id: str) -> Meeting | None: ...

    async def meeting_tdocs(self, meeting_id: str) -> list[TDoc]: ...

    async def meeting_tdoc_counts(self, meeting_ids: list[str]) -> dict[str, int]: ...

    async def get_tdoc(self, tdoc_id: str) -> TDoc | None: ...

    async def evidence(self, ids: list[str]) -> list[EvidenceRef]: ...

    async def document_blocks(
        self,
        document_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[DocumentBlock]: ...

    async def document_section_tree(self, document_id: str) -> list[DocumentSectionNode]: ...

    async def search_passages(
        self,
        query: str,
        *,
        tdoc_ids: list[str],
        meeting_ids: list[str],
        top_k: int,
        query_embedding: list[float] | None = None,
        embedding_profile_id: str | None = None,
    ) -> list[Passage]: ...


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        offset = int(payload["offset"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if offset < 0:
        raise ValueError("invalid cursor")
    return offset


def _section_id(document_id: str, path: tuple[str, ...]) -> str:
    digest = hashlib.sha256(f"{document_id}|{'/'.join(path)}".encode()).hexdigest()[:20]
    return f"section-{digest}"


def _build_section_tree(
    document_id: str,
    rows: Iterable[tuple[int, list[str]]],
) -> list[DocumentSectionNode]:
    sections: dict[tuple[str, ...], dict[str, int]] = {}
    for block_index, raw_path in rows:
        path = tuple(part.strip() for part in (raw_path or []) if part and part.strip())
        for depth in range(0, len(path) + 1):
            prefix = path[:depth]
            if prefix not in sections:
                sections[prefix] = {
                    "start": block_index,
                    "end": block_index,
                    "direct": 0,
                    "descendants": 1,
                }
            else:
                sections[prefix]["end"] = block_index
                sections[prefix]["descendants"] += 1
        sections[path]["direct"] += 1
    child_counts = Counter(path[:-1] for path in sections if path)
    tree: list[DocumentSectionNode] = []
    for path, stats in sorted(
        sections.items(), key=lambda item: (item[1]["start"], len(item[0]), item[0])
    ):
        title = path[-1] if path else "Document"
        page_match = re.fullmatch(r"Page\s+(\d+)", title, flags=re.IGNORECASE)
        tree.append(
            DocumentSectionNode(
                id=_section_id(document_id, path),
                parent_id=_section_id(document_id, path[:-1]) if path else None,
                title=title,
                section_path=list(path),
                depth=len(path),
                start_block_index=stats["start"],
                end_block_index=stats["end"],
                direct_block_count=stats["direct"],
                descendant_block_count=stats["descendants"],
                child_count=child_counts[path],
                page_number=int(page_match.group(1)) if page_match else None,
            )
        )
    return tree


class SqlRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        retrieval: RetrievalConfig | None = None,
        preview_dataset_version: str | None = None,
    ) -> None:
        self.sessions = sessions
        self.preview_dataset_version = preview_dataset_version
        self.retrieval = retrieval or RetrievalConfig(
            default_last_k_meetings=3,
            default_top_k=10,
            max_top_k=100,
            graph_hops=2,
            rrf_k=60,
            vector_weight=1.0,
            lexical_weight=1.0,
        )

    async def active_dataset_version(self) -> str:
        async with self.sessions() as session:
            if self.preview_dataset_version:
                result = await session.scalar(
                    select(DatasetVersionRow.id).where(
                        DatasetVersionRow.id == self.preview_dataset_version
                    )
                )
            else:
                result = await session.scalar(
                    select(DatasetVersionRow.id)
                    .where(DatasetVersionRow.is_active.is_(True))
                    .limit(1)
                )
        if not result:
            if self.preview_dataset_version:
                raise RuntimeError(
                    f"preview dataset version {self.preview_dataset_version} does not exist"
                )
            raise RuntimeError("no active dataset version")
        return result

    async def active_embedding_profile(self) -> EmbeddingProfileInfo | None:
        version = await self.active_dataset_version()
        async with self.sessions() as session:
            row = await session.execute(
                select(DatasetEmbeddingProfileRow, EmbeddingProfileRow)
                .join(
                    EmbeddingProfileRow,
                    EmbeddingProfileRow.id == DatasetEmbeddingProfileRow.profile_id,
                )
                .where(
                    DatasetEmbeddingProfileRow.dataset_version_id == version,
                    DatasetEmbeddingProfileRow.is_active.is_(True),
                )
                .limit(1)
            )
            result = row.first()
        if result is None:
            return None
        assignment, profile = result
        return EmbeddingProfileInfo(
            id=profile.id,
            provider=profile.provider,
            model=profile.model,
            revision=profile.revision,
            dimensions=profile.dimensions,
            state=assignment.state,
            embedded_chunks=assignment.embedded_chunks,
            total_chunks=assignment.total_chunks,
        )

    async def embedding_status(self) -> dict[str, Any]:
        profile = await self.active_embedding_profile()
        return {
            "active": profile is not None,
            "profile": profile.model_dump(mode="json") if profile else None,
            "coverage": (
                profile.embedded_chunks / profile.total_chunks
                if profile and profile.total_chunks
                else 0.0
            ),
        }

    async def list_meetings(
        self, working_groups: list[str], request: SearchRequest
    ) -> tuple[list[Meeting], str | None]:
        version = await self.active_dataset_version()
        offset = decode_cursor(request.cursor)
        limit = request.top_k
        filters = [MeetingRow.dataset_version_id == version]
        if working_groups:
            filters.append(MeetingRow.working_group_id.in_(working_groups))
        temporal = request.filters.temporal
        if temporal.meeting_ids:
            filters.append(MeetingRow.id.in_(temporal.meeting_ids))
        if temporal.duration_months:
            filters.append(
                MeetingRow.ends_on >= date.today() - timedelta(days=temporal.duration_months * 31)
            )
        if temporal.date_from:
            filters.append(MeetingRow.ends_on >= temporal.date_from)
        if temporal.date_to:
            filters.append(MeetingRow.starts_on <= temporal.date_to)
        query = (
            select(MeetingRow)
            .where(and_(*filters))
            .order_by(desc(MeetingRow.ends_on), desc(MeetingRow.number))
        )
        if temporal.last_k_meetings:
            limit = min(limit, temporal.last_k_meetings)
        async with self.sessions() as session:
            rows = list((await session.scalars(query.offset(offset).limit(limit + 1))).all())
        more = len(rows) > limit
        return [self._meeting(row) for row in rows[:limit]], encode_cursor(
            offset + limit
        ) if more else None

    async def search_tdocs(self, request: SearchRequest) -> tuple[list[TDoc], str | None]:
        version = await self.active_dataset_version()
        offset = decode_cursor(request.cursor)
        filters = [TDocRow.dataset_version_id == version]
        query_text = request.query.strip().lower()
        if query_text:
            pattern = f"%{query_text}%"
            filters.append(
                or_(
                    func.lower(TDocRow.id).like(pattern),
                    func.lower(TDocRow.title).like(pattern),
                    func.lower(TDocRow.abstract).like(pattern),
                    func.lower(TDocRow.summary).like(pattern),
                    func.lower(TDocRow.discussion).like(pattern),
                )
            )
        if request.filters.statuses:
            filters.append(
                TDocRow.status.in_([status.value for status in request.filters.statuses])
            )
        temporal = request.filters.temporal
        meeting_filters = [MeetingRow.dataset_version_id == version]
        if request.filters.working_groups:
            meeting_filters.append(MeetingRow.working_group_id.in_(request.filters.working_groups))
        if temporal.meeting_ids:
            meeting_filters.append(MeetingRow.id.in_(temporal.meeting_ids))
        if temporal.duration_months:
            meeting_filters.append(
                MeetingRow.ends_on >= date.today() - timedelta(days=temporal.duration_months * 31)
            )
        if temporal.date_from:
            meeting_filters.append(MeetingRow.ends_on >= temporal.date_from)
        if temporal.date_to:
            meeting_filters.append(MeetingRow.starts_on <= temporal.date_to)
        meeting_ids = select(MeetingRow.id).where(and_(*meeting_filters))
        if temporal.last_k_meetings:
            meeting_ids = meeting_ids.order_by(
                desc(MeetingRow.ends_on), desc(MeetingRow.number)
            ).limit(temporal.last_k_meetings)
        if request.filters.working_groups or any(
            (
                temporal.meeting_ids,
                temporal.last_k_meetings,
                temporal.duration_months,
                temporal.date_from,
                temporal.date_to,
            )
        ):
            filters.append(TDocRow.meeting_id.in_(meeting_ids))
        facet_filters = [
            func.lower(TDocRow.source).like(f"%{company.lower()}%")
            for company in request.filters.companies
        ]
        facet_filters.extend(
            func.lower(cast(TDocRow.releases, Text)).like(f"%{release.lower()}%")
            for release in request.filters.releases
        )
        facet_filters.extend(
            func.lower(cast(TDocRow.specifications, Text)).like(f"%{specification.lower()}%")
            for specification in request.filters.specifications
        )
        facet_filters.extend(
            func.lower(TDocRow.agenda_description).like(f"%{topic.lower()}%")
            for topic in request.filters.topics
        )
        if facet_filters:
            filters.append(
                and_(*facet_filters)
                if request.filters.match_mode == MatchMode.ALL
                else or_(*facet_filters)
            )
        query = select(TDocRow).where(and_(*filters)).order_by(TDocRow.id)
        async with self.sessions() as session:
            rows = list(
                (await session.scalars(query.offset(offset).limit(request.top_k + 1))).all()
            )
        more = len(rows) > request.top_k
        return [self._tdoc(row) for row in rows[: request.top_k]], encode_cursor(
            offset + request.top_k
        ) if more else None

    async def meeting(self, meeting_id: str) -> Meeting | None:
        version = await self.active_dataset_version()
        async with self.sessions() as session:
            row = await session.scalar(
                select(MeetingRow).where(
                    MeetingRow.dataset_version_id == version,
                    func.lower(MeetingRow.id) == meeting_id.lower(),
                )
            )
        return self._meeting(row) if row else None

    async def meeting_tdocs(self, meeting_id: str) -> list[TDoc]:
        version = await self.active_dataset_version()
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(TDocRow)
                        .where(
                            TDocRow.dataset_version_id == version,
                            func.lower(TDocRow.meeting_id) == meeting_id.lower(),
                        )
                        .order_by(TDocRow.id)
                    )
                ).all()
            )
        return [self._tdoc(row) for row in rows]

    async def meeting_tdoc_counts(self, meeting_ids: list[str]) -> dict[str, int]:
        if not meeting_ids:
            return {}
        version = await self.active_dataset_version()
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(TDocRow.meeting_id, func.count(TDocRow.row_id))
                    .where(
                        TDocRow.dataset_version_id == version,
                        TDocRow.meeting_id.in_(meeting_ids),
                    )
                    .group_by(TDocRow.meeting_id)
                )
            ).all()
        return {meeting_id: int(count) for meeting_id, count in rows}

    async def get_tdoc(self, tdoc_id: str) -> TDoc | None:
        version = await self.active_dataset_version()
        async with self.sessions() as session:
            row = await session.scalar(
                select(TDocRow).where(
                    TDocRow.dataset_version_id == version,
                    func.lower(TDocRow.id) == tdoc_id.lower(),
                )
            )
        return self._tdoc(row) if row else None

    async def evidence(self, ids: list[str]) -> list[EvidenceRef]:
        if not ids:
            return []
        version = await self.active_dataset_version()
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(EvidenceRow).where(
                            EvidenceRow.dataset_version_id == version, EvidenceRow.id.in_(ids)
                        )
                    )
                ).all()
            )
        return [
            EvidenceRef.model_validate(
                {
                    column.name: getattr(row, column.name)
                    for column in EvidenceRow.__table__.columns
                    if column.name not in {"dataset_version_id"}
                }
            )
            for row in rows
        ]

    async def document_blocks(
        self,
        document_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[DocumentBlock]:
        version = await self.active_dataset_version()
        query = (
            select(DocumentBlockRow)
            .where(
                DocumentBlockRow.dataset_version_id == version,
                DocumentBlockRow.document_id == document_id,
            )
            .order_by(DocumentBlockRow.block_index)
            .offset(offset)
        )
        if limit is not None:
            query = query.limit(limit)
        async with self.sessions() as session:
            rows = list((await session.scalars(query)).all())
        return [
            DocumentBlock(
                id=row.id,
                document_id=row.document_id,
                index=row.block_index,
                kind=BlockKind(row.kind),
                text=row.text,
                section_path=row.section_path,
                table_row=row.table_row,
            )
            for row in rows
        ]

    async def document_section_tree(self, document_id: str) -> list[DocumentSectionNode]:
        version = await self.active_dataset_version()
        async with self.sessions() as session:
            rows = list(
                (
                    await session.execute(
                        select(
                            DocumentBlockRow.block_index,
                            DocumentBlockRow.section_path,
                        )
                        .where(
                            DocumentBlockRow.dataset_version_id == version,
                            DocumentBlockRow.document_id == document_id,
                        )
                        .order_by(DocumentBlockRow.block_index)
                    )
                ).all()
            )
        return _build_section_tree(document_id, [(row[0], row[1]) for row in rows])

    async def search_passages(
        self,
        query: str,
        *,
        tdoc_ids: list[str],
        meeting_ids: list[str],
        top_k: int,
        query_embedding: list[float] | None = None,
        embedding_profile_id: str | None = None,
    ) -> list[Passage]:
        version = await self.active_dataset_version()
        active_profile = (
            await self.active_embedding_profile() if query_embedding is not None else None
        )
        if query_embedding is not None and (
            active_profile is None
            or (embedding_profile_id is not None and active_profile.id != embedding_profile_id)
            or len(query_embedding) != active_profile.dimensions
        ):
            query_embedding = None
        filters = [RetrievalChunkRow.dataset_version_id == version]
        document_ids = list(tdoc_ids)
        if meeting_ids:
            async with self.sessions() as session:
                meeting_tdocs = list(
                    (
                        await session.scalars(
                            select(TDocRow.id).where(
                                TDocRow.dataset_version_id == version,
                                TDocRow.meeting_id.in_(meeting_ids),
                            )
                        )
                    ).all()
                )
            document_ids.extend(meeting_tdocs)
        if document_ids:
            filters.append(RetrievalChunkRow.document_id.in_(set(document_ids)))
        async with self.sessions() as session:
            if session.bind and session.bind.dialect.name == "postgresql":
                return await self._postgres_passages(
                    session, filters, query, top_k, query_embedding, active_profile
                )
            rows = list(
                (
                    await session.scalars(
                        select(RetrievalChunkRow).where(and_(*filters)).limit(5000)
                    )
                ).all()
            )
        chunks = [
            RetrievalChunk(
                id=row.id,
                document_id=row.document_id,
                block_ids=row.block_ids,
                text=row.text,
                section_path=row.section_path,
                token_count=row.token_count,
                embedding=None,
                evidence_ids=row.evidence_ids,
            )
            for row in rows
        ]
        return rank_chunks(
            chunks,
            query,
            top_k=top_k,
            query_embedding=query_embedding,
            lexical_weight=self.retrieval.lexical_weight,
            vector_weight=self.retrieval.vector_weight,
            rrf_k=self.retrieval.rrf_k,
        )

    async def _postgres_passages(
        self,
        session: AsyncSession,
        filters: list[Any],
        query: str,
        top_k: int,
        query_embedding: list[float] | None,
        active_profile: EmbeddingProfileInfo | None,
    ) -> list[Passage]:
        candidate_limit = min(max(top_k * 10, 50), 1000)
        rankings: list[tuple[list[str], float]] = []
        if query.strip():
            language: ColumnElement[Any] = literal_column("'english'")
            search_vector = func.to_tsvector(language, RetrievalChunkRow.text)
            search_query = func.websearch_to_tsquery(language, query)
            lexical_ids = list(
                (
                    await session.scalars(
                        select(RetrievalChunkRow.id)
                        .where(and_(*filters), search_vector.op("@@")(search_query))
                        .order_by(desc(func.ts_rank_cd(search_vector, search_query)))
                        .limit(candidate_limit)
                    )
                ).all()
            )
            rankings.append((lexical_ids, self.retrieval.lexical_weight))
        if query_embedding is not None and active_profile is not None:
            vector_expression = cast(ChunkEmbeddingRow.embedding, Vector(active_profile.dimensions))
            vector_ids = list(
                (
                    await session.scalars(
                        select(RetrievalChunkRow.id)
                        .join(
                            ChunkEmbeddingRow,
                            and_(
                                ChunkEmbeddingRow.dataset_version_id
                                == RetrievalChunkRow.dataset_version_id,
                                ChunkEmbeddingRow.chunk_id == RetrievalChunkRow.id,
                            ),
                        )
                        .where(
                            and_(*filters),
                            ChunkEmbeddingRow.profile_id == active_profile.id,
                            ChunkEmbeddingRow.dimensions == active_profile.dimensions,
                        )
                        .order_by(vector_expression.cosine_distance(query_embedding))
                        .limit(candidate_limit)
                    )
                ).all()
            )
            rankings.append((vector_ids, self.retrieval.vector_weight))
        if not rankings:
            identifiers = list(
                (
                    await session.scalars(
                        select(RetrievalChunkRow.id)
                        .where(and_(*filters))
                        .order_by(RetrievalChunkRow.id)
                        .limit(top_k)
                    )
                ).all()
            )
            rankings.append((identifiers, 1.0))
        fused = reciprocal_rank_fusion(rankings, self.retrieval.rrf_k)
        if not fused:
            return []
        rows = list(
            (
                await session.scalars(
                    select(RetrievalChunkRow).where(and_(*filters), RetrievalChunkRow.id.in_(fused))
                )
            ).all()
        )
        evidence_ids = {evidence_id for row in rows for evidence_id in (row.evidence_ids or [])}
        authority: dict[str, float] = {}
        if evidence_ids:
            evidence_rows = list(
                (
                    await session.scalars(
                        select(EvidenceRow).where(EvidenceRow.id.in_(evidence_ids))
                    )
                ).all()
            )
            authority = {
                row.id: AUTHORITY_RANK[EvidenceAuthority(row.authority)] / 100
                for row in evidence_rows
            }
        for row in rows:
            multiplier = max(
                (authority.get(item, 1.0) for item in row.evidence_ids or []),
                default=1.0,
            )
            fused[row.id] *= multiplier
        by_id = {row.id: row for row in rows}
        ordered = sorted(by_id, key=lambda identifier: (-fused[identifier], identifier))[:top_k]
        return [
            Passage(
                chunk_id=identifier,
                document_id=by_id[identifier].document_id,
                text=by_id[identifier].text,
                section_path=by_id[identifier].section_path,
                block_ids=by_id[identifier].block_ids,
                score=fused[identifier],
                evidence_ids=by_id[identifier].evidence_ids,
            )
            for identifier in ordered
        ]

    @staticmethod
    def _meeting(row: MeetingRow) -> Meeting:
        return Meeting(
            id=row.id,
            working_group_id=row.working_group_id,
            number=row.number,
            variant=row.variant,
            name=row.name,
            source_url=row.source_url,
            starts_on=row.starts_on,
            ends_on=row.ends_on,
            readiness=row.readiness,
        )

    @staticmethod
    def _tdoc(row: TDocRow) -> TDoc:
        return TDoc(
            id=row.id,
            meeting_id=row.meeting_id,
            title=row.title,
            source=row.source,
            document_type=row.document_type,
            purpose=row.purpose,
            agenda_item=row.agenda_item,
            agenda_description=row.agenda_description,
            status=Conclusion(row.status),
            status_raw=row.status_raw,
            abstract=row.abstract,
            summary=row.summary,
            discussion=row.discussion,
            conclusion_text=row.conclusion_text,
            revised_from=row.revised_from,
            revised_to=row.revised_to,
            releases=row.releases or [],
            specifications=row.specifications or [],
            work_items=row.work_items or [],
            cr_number=row.cr_number,
            cr_revision=row.cr_revision,
            cr_category=row.cr_category,
            source_url=row.source_url,
            evidence_ids=row.evidence_ids or [],
        )


class InMemoryRepository:
    def __init__(
        self,
        meetings: list[Meeting] | None = None,
        tdocs: list[TDoc] | None = None,
        evidence: list[EvidenceRef] | None = None,
        chunks: list[RetrievalChunk] | None = None,
        blocks: list[DocumentBlock] | None = None,
        dataset_version: str = "dev-fixture-v1",
    ) -> None:
        self.meetings = meetings or []
        self.tdocs = tdocs or []
        self.evidence_map = {item.id: item for item in evidence or []}
        self.chunks = chunks or []
        self.blocks = blocks or []
        self.dataset_version = dataset_version

    async def active_dataset_version(self) -> str:
        return self.dataset_version

    async def active_embedding_profile(self) -> EmbeddingProfileInfo | None:
        dimensions = next(
            (len(chunk.embedding) for chunk in self.chunks if chunk.embedding is not None), None
        )
        if dimensions is None:
            return None
        return EmbeddingProfileInfo(
            id="fixture-embedding",
            provider="fixture",
            model="fixture",
            revision="fixture",
            dimensions=dimensions,
            embedded_chunks=sum(chunk.embedding is not None for chunk in self.chunks),
            total_chunks=len(self.chunks),
        )

    async def embedding_status(self) -> dict[str, Any]:
        profile = await self.active_embedding_profile()
        return {
            "active": profile is not None,
            "profile": profile.model_dump(mode="json") if profile else None,
            "coverage": (
                profile.embedded_chunks / profile.total_chunks
                if profile and profile.total_chunks
                else 0.0
            ),
        }

    async def list_meetings(
        self, working_groups: list[str], request: SearchRequest
    ) -> tuple[list[Meeting], str | None]:
        values = [
            m for m in self.meetings if not working_groups or m.working_group_id in working_groups
        ]
        temporal = request.filters.temporal
        if temporal.meeting_ids:
            values = [m for m in values if m.id in temporal.meeting_ids]
        if temporal.duration_months:
            cutoff = date.today() - timedelta(days=31 * temporal.duration_months)
            values = [m for m in values if m.ends_on and m.ends_on >= cutoff]
        if temporal.date_from:
            values = [m for m in values if m.ends_on and m.ends_on >= temporal.date_from]
        if temporal.date_to:
            values = [m for m in values if m.starts_on and m.starts_on <= temporal.date_to]
        values.sort(key=lambda m: (m.ends_on or date.min, m.number), reverse=True)
        if temporal.last_k_meetings:
            values = values[: temporal.last_k_meetings]
        return self._page(values, request)

    async def search_tdocs(self, request: SearchRequest) -> tuple[list[TDoc], str | None]:
        values = list(self.tdocs)
        query = request.query.strip().lower()
        if query:
            values = [
                tdoc
                for tdoc in values
                if query
                in " ".join(
                    (tdoc.id, tdoc.title, tdoc.abstract, tdoc.summary, tdoc.discussion)
                ).lower()
            ]
        filters = request.filters
        if filters.working_groups:
            meeting_wgs = {m.id: m.working_group_id for m in self.meetings}
            values = [t for t in values if meeting_wgs.get(t.meeting_id) in filters.working_groups]
        if filters.statuses:
            values = [t for t in values if t.status in filters.statuses]
        temporal_meeting_ids = self._temporal_meeting_ids(filters)
        if temporal_meeting_ids is not None:
            values = [t for t in values if t.meeting_id in temporal_meeting_ids]
        facet_needles = [
            *filters.companies,
            *filters.releases,
            *filters.specifications,
            *filters.topics,
        ]
        if facet_needles:
            values = [
                tdoc
                for tdoc in values
                if _matches(
                    [
                        tdoc.source,
                        *tdoc.releases,
                        *tdoc.specifications,
                        tdoc.agenda_description,
                    ],
                    facet_needles,
                    filters.match_mode,
                )
            ]
        values.sort(key=lambda item: item.id)
        return self._page(values, request)

    async def meeting(self, meeting_id: str) -> Meeting | None:
        return next(
            (item for item in self.meetings if item.id.casefold() == meeting_id.casefold()), None
        )

    async def meeting_tdocs(self, meeting_id: str) -> list[TDoc]:
        return sorted(
            (item for item in self.tdocs if item.meeting_id.casefold() == meeting_id.casefold()),
            key=lambda item: item.id,
        )

    async def meeting_tdoc_counts(self, meeting_ids: list[str]) -> dict[str, int]:
        selected = set(meeting_ids)
        return dict(Counter(item.meeting_id for item in self.tdocs if item.meeting_id in selected))

    def _temporal_meeting_ids(self, filters: SearchFilters) -> set[str] | None:
        search_filters = filters
        temporal = search_filters.temporal
        meetings = list(self.meetings)
        if search_filters.working_groups:
            meetings = [
                item for item in meetings if item.working_group_id in search_filters.working_groups
            ]
        if temporal.meeting_ids:
            meetings = [item for item in meetings if item.id in temporal.meeting_ids]
        if temporal.duration_months:
            cutoff = date.today() - timedelta(days=31 * temporal.duration_months)
            meetings = [item for item in meetings if item.ends_on and item.ends_on >= cutoff]
        if temporal.date_from:
            meetings = [
                item for item in meetings if item.ends_on and item.ends_on >= temporal.date_from
            ]
        if temporal.date_to:
            meetings = [
                item for item in meetings if item.starts_on and item.starts_on <= temporal.date_to
            ]
        meetings.sort(key=lambda item: (item.ends_on or date.min, item.number), reverse=True)
        if temporal.last_k_meetings:
            meetings = meetings[: temporal.last_k_meetings]
        has_constraint = bool(search_filters.working_groups) or any(
            (
                temporal.meeting_ids,
                temporal.last_k_meetings,
                temporal.duration_months,
                temporal.date_from,
                temporal.date_to,
            )
        )
        return {item.id for item in meetings} if has_constraint else None

    async def get_tdoc(self, tdoc_id: str) -> TDoc | None:
        return next((item for item in self.tdocs if item.id.lower() == tdoc_id.lower()), None)

    async def evidence(self, ids: list[str]) -> list[EvidenceRef]:
        return [self.evidence_map[item_id] for item_id in ids if item_id in self.evidence_map]

    async def document_blocks(
        self,
        document_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[DocumentBlock]:
        blocks = sorted(
            (block for block in self.blocks if block.document_id == document_id),
            key=lambda block: block.index,
        )
        return blocks[offset:] if limit is None else blocks[offset : offset + limit]

    async def document_section_tree(self, document_id: str) -> list[DocumentSectionNode]:
        rows = [
            (block.index, block.section_path)
            for block in sorted(self.blocks, key=lambda item: item.index)
            if block.document_id == document_id
        ]
        return _build_section_tree(document_id, rows)

    async def search_passages(
        self,
        query: str,
        *,
        tdoc_ids: list[str],
        meeting_ids: list[str],
        top_k: int,
        query_embedding: list[float] | None = None,
        embedding_profile_id: str | None = None,
    ) -> list[Passage]:
        del embedding_profile_id
        allowed_tdocs = set(tdoc_ids)
        if meeting_ids:
            allowed_tdocs.update(
                item.id for item in self.tdocs if item.meeting_id in set(meeting_ids)
            )
        chunks = [
            chunk
            for chunk in self.chunks
            if not allowed_tdocs or chunk.document_id in allowed_tdocs
        ]
        return rank_chunks(chunks, query, top_k=top_k, query_embedding=query_embedding)

    def revision_chain(self, tdoc_id: str) -> list[str]:
        by_id = {tdoc.id: tdoc for tdoc in self.tdocs}
        if tdoc_id not in by_id:
            return []
        seen: set[str] = set()
        current = by_id[tdoc_id]
        while current.revised_from and current.revised_from in by_id and current.id not in seen:
            seen.add(current.id)
            current = by_id[current.revised_from]
        chain: list[str] = []
        seen.clear()
        while current.id not in seen:
            seen.add(current.id)
            chain.append(current.id)
            if not current.revised_to or current.revised_to not in by_id:
                break
            current = by_id[current.revised_to]
        return chain

    def status_counts(self, meeting_id: str) -> dict[str, int]:
        return dict(
            Counter(tdoc.status.value for tdoc in self.tdocs if tdoc.meeting_id == meeting_id)
        )

    def company_counts(self, meeting_id: str) -> list[dict[str, int | str]]:
        counter: Counter[str] = Counter()
        for tdoc in self.tdocs:
            if tdoc.meeting_id == meeting_id:
                for source in (part.strip() for part in tdoc.source.split(",")):
                    if source:
                        counter[source] += 1
        return [
            {"company": company, "tdoc_count": count} for company, count in counter.most_common()
        ]

    def topic_counts(self, meeting_id: str) -> list[dict[str, int | str]]:
        counter: Counter[str] = Counter()
        for tdoc in self.tdocs:
            if tdoc.meeting_id == meeting_id and tdoc.agenda_description:
                counter[tdoc.agenda_description] += 1
        return [{"topic": topic, "tdoc_count": count} for topic, count in counter.most_common()]

    @staticmethod
    def _page(values: list[PageItem], request: SearchRequest) -> tuple[list[PageItem], str | None]:
        offset = decode_cursor(request.cursor)
        page = values[offset : offset + request.top_k]
        next_cursor = (
            encode_cursor(offset + request.top_k) if offset + request.top_k < len(values) else None
        )
        return page, next_cursor


def _matches(haystack: list[str], needles: list[str], mode: MatchMode) -> bool:
    text = " ".join(haystack).lower()
    checks = [needle.lower() in text for needle in needles]
    return all(checks) if mode == MatchMode.ALL else any(checks)


PageItem = TypeVar("PageItem")
