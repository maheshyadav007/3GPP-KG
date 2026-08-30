from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .constants import BlockKind, Conclusion, EvidenceAuthority, MatchMode


class TemporalScope(BaseModel):
    meeting_ids: list[str] | None = None
    last_k_meetings: int | None = Field(None, ge=1)
    duration_months: int | None = Field(None, ge=1)
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def one_selector(self) -> TemporalScope:
        selectors = [
            bool(self.meeting_ids),
            self.last_k_meetings is not None,
            self.duration_months is not None,
            self.date_from is not None or self.date_to is not None,
        ]
        if sum(selectors) > 1:
            raise ValueError(
                "use exactly one of meeting_ids, last_k_meetings, duration_months, or date range"
            )
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from cannot be after date_to")
        return self


class EvidenceRef(BaseModel):
    id: str
    source_url: str
    artifact_sha256: str
    authority: EvidenceAuthority
    meeting_id: str | None = None
    tdoc_id: str | None = None
    section_path: list[str] = Field(default_factory=list)
    block_id: str | None = None
    excerpt: str | None = None
    extraction_method: str = "parser"
    extractor_version: str = "0.1.0"
    confidence: float = Field(1.0, ge=0, le=1)
    meeting_time: date | None = None
    observed_at: datetime | None = None


class WorkingGroup(BaseModel):
    id: str
    name: str
    tsg: str


class Meeting(BaseModel):
    id: str
    working_group_id: str
    number: int
    variant: str = ""
    name: str
    source_url: str
    starts_on: date | None = None
    ends_on: date | None = None
    readiness: str = "discovered"


class TDoc(BaseModel):
    id: str
    meeting_id: str
    title: str
    source: str = ""
    document_type: str = ""
    purpose: str = ""
    agenda_item: str = ""
    agenda_description: str = ""
    status: Conclusion = Conclusion.UNKNOWN
    status_raw: str = ""
    abstract: str = ""
    summary: str = ""
    discussion: str = ""
    conclusion_text: str = ""
    revised_from: str | None = None
    revised_to: str | None = None
    releases: list[str] = Field(default_factory=list)
    specifications: list[str] = Field(default_factory=list)
    work_items: list[str] = Field(default_factory=list)
    cr_number: str | None = None
    cr_revision: str | None = None
    cr_category: str | None = None
    source_url: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class DocumentBlock(BaseModel):
    id: str
    document_id: str
    index: int = Field(ge=0)
    kind: BlockKind
    text: str
    section_path: list[str] = Field(default_factory=list)
    table_row: int | None = None


class TDocDetail(BaseModel):
    tdoc: TDoc
    blocks: list[DocumentBlock] = Field(default_factory=list)


class DocumentSectionNode(BaseModel):
    id: str
    parent_id: str | None = None
    title: str
    section_path: list[str] = Field(default_factory=list)
    depth: int = Field(ge=0)
    start_block_index: int = Field(ge=0)
    end_block_index: int = Field(ge=0)
    direct_block_count: int = Field(ge=0)
    descendant_block_count: int = Field(ge=1)
    child_count: int = Field(ge=0)
    page_number: int | None = Field(default=None, ge=1)


class RetrievalChunk(BaseModel):
    id: str
    document_id: str
    block_ids: list[str]
    text: str
    section_path: list[str] = Field(default_factory=list)
    token_count: int = Field(ge=0)
    embedding: list[float] | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Passage(BaseModel):
    chunk_id: str
    document_id: str
    text: str
    section_path: list[str] = Field(default_factory=list)
    block_ids: list[str] = Field(default_factory=list)
    score: float = Field(ge=0)
    evidence_ids: list[str] = Field(default_factory=list)


class SearchFilters(BaseModel):
    working_groups: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    releases: list[str] = Field(default_factory=list)
    specifications: list[str] = Field(default_factory=list)
    statuses: list[Conclusion] = Field(default_factory=list)
    match_mode: MatchMode = MatchMode.ALL
    temporal: TemporalScope = Field(default_factory=TemporalScope)


class SearchRequest(BaseModel):
    query: str = ""
    filters: SearchFilters = Field(default_factory=SearchFilters)
    top_k: int = Field(10, ge=1, le=100)
    cursor: str | None = None


class NewsletterPacket(BaseModel):
    meeting: Meeting
    edition: Literal["provisional", "final"]
    generated_at: datetime
    totals: dict[str, int]
    decisions: dict[str, list[TDoc]]
    hot_topics: list[dict[str, Any]]
    company_activity: list[dict[str, Any]]
    revision_chains: list[list[str]]
    affected_specs: list[dict[str, Any]]
    evidence_ids: list[str]


class Envelope[T](BaseModel):
    data: T
    evidence: list[EvidenceRef] = Field(default_factory=list)
    dataset_version: str
    completeness: Literal["complete", "partial", "unavailable"] = "complete"
    confidence: float = Field(1.0, ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)
    next_cursor: str | None = None
