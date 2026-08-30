from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from .config import load_organization_aliases
from .constants import MatchMode
from .domain import Meeting, TDoc
from .ingestion.normalize import normalize_organization, split_organization_sources

FacetKind = Literal["company", "topic", "specification"]


def canonical_identifier(value: str) -> str:
    identifier = "-".join(value.lower().replace("/", " ").split())
    if len(identifier) <= 80:
        return identifier
    digest = hashlib.sha256(identifier.encode()).hexdigest()[:16]
    return f"{identifier[:63]}-{digest}"


def typed_id(entity_type: str, identifier: str) -> str:
    return f"{entity_type}:{identifier}"


@dataclass(frozen=True, slots=True)
class TDocFacets:
    companies: frozenset[str]
    topics: frozenset[str]
    specifications: frozenset[str]


def tdoc_facets(tdoc: TDoc) -> TDocFacets:
    aliases = load_organization_aliases()
    companies = {
        canonical_identifier(normalize_organization(company, aliases))
        for company in split_organization_sources(tdoc.source)
    }
    topics = (
        {canonical_identifier(tdoc.agenda_description)} if tdoc.agenda_description else set()
    )
    specifications = {canonical_identifier(value) for value in tdoc.specifications}
    return TDocFacets(
        companies=frozenset(companies),
        topics=frozenset(topics),
        specifications=frozenset(specifications),
    )


def filter_tdocs(
    tdocs: list[TDoc],
    *,
    query: str,
    company_ids: list[str],
    topic_ids: list[str],
    specification_ids: list[str],
    match_mode: MatchMode,
) -> list[TDoc]:
    query = query.strip().casefold()
    has_facets = bool(company_ids or topic_ids or specification_ids)
    result: list[TDoc] = []
    for tdoc in tdocs:
        if query and query not in " ".join(
            (tdoc.id, tdoc.title, tdoc.abstract, tdoc.summary, tdoc.discussion)
        ).casefold():
            continue
        if has_facets:
            facets = tdoc_facets(tdoc)
            matches = [value in facets.companies for value in company_ids]
            matches.extend(value in facets.topics for value in topic_ids)
            matches.extend(value in facets.specifications for value in specification_ids)
            if match_mode == MatchMode.ALL and not all(matches):
                continue
            if match_mode == MatchMode.ANY and not any(matches):
                continue
        result.append(tdoc)
    return result


def facet_options(
    tdocs: list[TDoc], facet: FacetKind, query: str, limit: int
) -> list[dict[str, Any]]:
    labels: dict[str, str] = {}
    counts: Counter[str] = Counter()
    aliases = load_organization_aliases()
    for tdoc in tdocs:
        values: list[str]
        if facet == "company":
            values = [
                normalize_organization(value, aliases)
                for value in split_organization_sources(tdoc.source)
            ]
        elif facet == "topic":
            values = [tdoc.agenda_description] if tdoc.agenda_description else []
        else:
            values = tdoc.specifications
        for label in dict.fromkeys(value.strip() for value in values if value.strip()):
            identifier = canonical_identifier(label)
            labels.setdefault(identifier, label)
            counts[identifier] += 1
    needle = query.strip().casefold()
    candidates = [identifier for identifier, label in labels.items() if needle in label.casefold()]

    def rank(identifier: str) -> tuple[int, int, str]:
        label = labels[identifier].casefold()
        match_rank = 0 if label == needle and needle else 1 if label.startswith(needle) else 2
        return match_rank, -counts[identifier], label

    return [
        {"id": identifier, "label": labels[identifier], "tdoc_count": counts[identifier]}
        for identifier in sorted(candidates, key=rank)[:limit]
    ]


def build_graph(meeting: Meeting, tdocs: list[TDoc]) -> dict[str, Any]:
    aliases = load_organization_aliases()
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    def add_node(
        entity_type: str,
        identifier: str,
        label: str,
        properties: dict[str, Any] | None = None,
        *,
        boundary: bool = False,
    ) -> str:
        key = typed_id(entity_type, identifier)
        candidate = {
            "id": key,
            "entity_id": identifier,
            "type": entity_type,
            "label": label,
            "properties": properties or {},
            "boundary": boundary,
        }
        existing = nodes.get(key)
        if existing is None or (existing["boundary"] and not boundary):
            nodes[key] = candidate
        return key

    def add_edge(
        source: str, target: str, predicate: str, evidence_ids: list[str]
    ) -> None:
        value = "|".join((source, predicate, target))
        identifier = "edge-" + hashlib.sha256(value.encode()).hexdigest()[:32]
        edges[identifier] = {
            "id": identifier,
            "source": source,
            "target": target,
            "type": predicate,
            "evidence_ids": evidence_ids,
        }

    meeting_node = add_node(
        "meeting",
        meeting.id,
        meeting.name,
        {"working_group": meeting.working_group_id, "readiness": meeting.readiness},
    )
    active_tdoc_ids = {tdoc.id for tdoc in tdocs}
    for tdoc in tdocs:
        tdoc_node = add_node(
            "tdoc", tdoc.id, tdoc.title or tdoc.id, {"status": tdoc.status.value}
        )
        add_edge(meeting_node, tdoc_node, "contains", tdoc.evidence_ids)
        for company in split_organization_sources(tdoc.source):
            label = normalize_organization(company, aliases)
            node = add_node("organization", canonical_identifier(label), label)
            add_edge(tdoc_node, node, "submitted_by", tdoc.evidence_ids)
        if tdoc.agenda_item or tdoc.agenda_description:
            agenda_id = (
                f"{meeting.id}:{canonical_identifier(tdoc.agenda_item or tdoc.agenda_description)}"
            )
            node = add_node(
                "agenda_item",
                agenda_id,
                tdoc.agenda_description or tdoc.agenda_item,
                {"number": tdoc.agenda_item},
            )
            add_edge(tdoc_node, node, "belongs_to_agenda", tdoc.evidence_ids)
        if tdoc.agenda_description:
            node = add_node(
                "topic",
                canonical_identifier(tdoc.agenda_description),
                tdoc.agenda_description,
            )
            add_edge(tdoc_node, node, "mentions_topic", tdoc.evidence_ids)
        for specification in tdoc.specifications:
            node = add_node(
                "specification", canonical_identifier(specification), specification
            )
            add_edge(tdoc_node, node, "affects_spec", tdoc.evidence_ids)
        for release in tdoc.releases:
            node = add_node("release", canonical_identifier(release), release)
            add_edge(tdoc_node, node, "targets_release", tdoc.evidence_ids)
        for work_item in tdoc.work_items:
            node = add_node("work_item", canonical_identifier(work_item), work_item)
            add_edge(tdoc_node, node, "related_to_work_item", tdoc.evidence_ids)
        if tdoc.cr_number:
            cr_id = f"{tdoc.meeting_id}:{canonical_identifier(tdoc.cr_number)}"
            node = add_node(
                "change_request",
                cr_id,
                f"CR {tdoc.cr_number}",
                {"revision": tdoc.cr_revision or "", "category": tdoc.cr_category or ""},
            )
            add_edge(tdoc_node, node, "has_change_request", tdoc.evidence_ids)
        if tdoc.revised_from:
            predecessor = add_node(
                "tdoc",
                tdoc.revised_from,
                tdoc.revised_from,
                {"placeholder": True},
                boundary=tdoc.revised_from not in active_tdoc_ids,
            )
            add_edge(tdoc_node, predecessor, "revises", tdoc.evidence_ids)
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}
