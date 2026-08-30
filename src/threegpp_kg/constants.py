from enum import StrEnum


class WorkingGroupId(StrEnum):
    RAN2 = "RAN2"
    RAN3 = "RAN3"
    SA2 = "SA2"
    CT1 = "CT1"


class ArtifactKind(StrEnum):
    AGENDA = "agenda"
    TDOC_LIST = "tdoc_list"
    TDOC = "tdoc"
    REPORT = "report"
    LIAISON = "liaison"
    OTHER = "other"


class Conclusion(StrEnum):
    AVAILABLE = "available"
    AGREED = "agreed"
    APPROVED = "approved"
    ENDORSED = "endorsed"
    MERGED = "merged"
    NOT_PURSUED = "not_pursued"
    NOT_TREATED = "not_treated"
    NOTED = "noted"
    POSTPONED = "postponed"
    REISSUED = "reissued"
    REJECTED = "rejected"
    RESERVED = "reserved"
    REVISED = "revised"
    WITHDRAWN = "withdrawn"
    UNKNOWN = "unknown"


class EvidenceAuthority(StrEnum):
    APPROVED_REPORT = "approved_report"
    FINAL_REPORT = "final_report"
    MEETING_EXPORT = "meeting_export"
    DRAFT_REPORT = "draft_report"
    TDOC_BODY = "tdoc_body"
    MODEL_INFERENCE = "model_inference"


AUTHORITY_RANK: dict[EvidenceAuthority, int] = {
    EvidenceAuthority.APPROVED_REPORT: 100,
    EvidenceAuthority.FINAL_REPORT: 90,
    EvidenceAuthority.MEETING_EXPORT: 80,
    EvidenceAuthority.DRAFT_REPORT: 70,
    EvidenceAuthority.TDOC_BODY: 60,
    EvidenceAuthority.MODEL_INFERENCE: 10,
}


MIN_DATASET_INGESTION_COVERAGE = 0.995


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE_ROW = "table_row"
    DISCUSSION = "discussion"
    AGREEMENT = "agreement"
    CONCLUSION = "conclusion"
    NOTE = "note"


class EdgeType(StrEnum):
    CONTAINS = "contains"
    SUBMITTED_BY = "submitted_by"
    BELONGS_TO_AGENDA = "belongs_to_agenda"
    REVISES = "revises"
    MERGED_INTO = "merged_into"
    REPLIES_TO = "replies_to"
    AFFECTS_SPEC = "affects_spec"
    TARGETS_RELEASE = "targets_release"
    RELATED_TO_WORK_ITEM = "related_to_work_item"
    MENTIONS_TOPIC = "mentions_topic"
    CONCLUDES_TDOC = "concludes_tdoc"
    REPORTS_ON = "reports_on"
    SUPERSEDES = "supersedes"
    HAS_CHANGE_REQUEST = "has_change_request"


class DatasetState(StrEnum):
    BUILDING = "building"
    VALIDATED = "validated"
    ACTIVE = "active"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class MeetingReadiness(StrEnum):
    DISCOVERED = "discovered"
    COLLECTING = "collecting"
    AWAITING_REPORT = "awaiting_report"
    PROVISIONAL_READY = "provisional_ready"
    FINAL_READY = "final_ready"
    ERROR = "error"


class MatchMode(StrEnum):
    ALL = "all"
    ANY = "any"


class Readiness(StrEnum):
    READY = "READY"
    READY_WITH_LIMITATIONS = "READY WITH LIMITATIONS"
    NOT_READY = "NOT READY"


STATUS_ALIASES: dict[str, Conclusion] = {
    "available": Conclusion.AVAILABLE,
    "agreed": Conclusion.AGREED,
    "approved": Conclusion.APPROVED,
    "endorsed": Conclusion.ENDORSED,
    "merged": Conclusion.MERGED,
    "not pursued": Conclusion.NOT_PURSUED,
    "not treated": Conclusion.NOT_TREATED,
    "not handled": Conclusion.NOT_TREATED,
    "noted": Conclusion.NOTED,
    "postponed": Conclusion.POSTPONED,
    "reissued": Conclusion.REISSUED,
    "rejected": Conclusion.REJECTED,
    "reserved": Conclusion.RESERVED,
    "revised": Conclusion.REVISED,
    "withdrawn": Conclusion.WITHDRAWN,
}


FORBIDDEN_WORKBOOK_SHEETS = frozenset(
    {
        "registration",
        "voting list",
        "cc_attendees",
        "meeting_admin",
    }
)
