from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, model_validator

from .config import FeatureConfig, NewsletterConfig
from .constants import (
    AUTHORITY_RANK,
    NEWSLETTER_PACKET_VERSION,
    NEWSLETTER_PROMPT_VERSION,
    Conclusion,
)
from .domain import (
    Envelope,
    EvidenceRef,
    Meeting,
    MeetingBriefing,
    MeetingObservation,
    NewsletterDelta,
    NewsletterPacket,
    NewsletterSignal,
    PacketEvidence,
    RevisionAnalysis,
    SignalScore,
    TDoc,
    TDocAppendixEntry,
    TechnicalImpact,
    TopicTrend,
)
from .models.client import OpenAICompatibleClient

DECISION_STATUSES = {
    Conclusion.AGREED,
    Conclusion.APPROVED,
    Conclusion.ENDORSED,
    Conclusion.MERGED,
    Conclusion.REJECTED,
    Conclusion.NOT_PURSUED,
    Conclusion.POSTPONED,
    Conclusion.WITHDRAWN,
}
UNRESOLVED_STATUSES = {
    Conclusion.AVAILABLE,
    Conclusion.NOT_TREATED,
    Conclusion.POSTPONED,
    Conclusion.REISSUED,
    Conclusion.RESERVED,
    Conclusion.REVISED,
    Conclusion.UNKNOWN,
}
NEGATIVE_STATUSES = {
    Conclusion.REJECTED,
    Conclusion.NOT_PURSUED,
    Conclusion.POSTPONED,
    Conclusion.WITHDRAWN,
}
POSITIVE_STATUSES = {Conclusion.AGREED, Conclusion.APPROVED, Conclusion.ENDORSED}
REQUIRED_RENDERED_SECTIONS = {
    "material_changes",
    "decisions",
    "topic_evolution",
    "technical_impact",
    "company_activity",
    "engineering_implications",
    "watch_items",
    "appendix_summary",
}


def _normalized_topic(tdoc: TDoc) -> str:
    value = tdoc.agenda_description.strip() or tdoc.title.strip() or "Unclassified"
    return re.sub(r"\s+", " ", value).casefold()


def _display_topic(tdocs: list[TDoc]) -> str:
    selected = min(
        (tdoc.agenda_description.strip() or tdoc.title.strip() for tdoc in tdocs),
        key=lambda value: (len(value), value.casefold()),
    )
    return re.sub(r"\s+", " ", selected)


def _companies(tdoc: TDoc) -> list[str]:
    return sorted(
        {item.strip() for item in re.split(r"[,;/]", tdoc.source) if item.strip()},
        key=lambda item: (item.casefold(), item),
    )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _signal_id(category: str, key: str, tdoc_ids: list[str]) -> str:
    body = f"{category}|{key}|{'|'.join(sorted(tdoc_ids))}"
    return f"signal-{hashlib.sha256(body.encode()).hexdigest()[:20]}"


def _observation_label(item: MeetingObservation) -> str:
    return {
        "decision": "Chair decision",
        "discussion_summary": "Chair discussion",
        "open_issue": "Open issue",
        "follow_up_action": "Follow-up action",
        "intended_outcome": "Intended outcome",
        "deadline": "Deadline",
        "dependency": "Dependency",
    }[item.observation_type]


def _score(
    tdocs: list[TDoc],
    *,
    evidence_by_id: dict[str, EvidenceRef],
    revision_depth: int = 1,
    novelty: float = 0,
    persistence: float = 0,
) -> SignalScore:
    authorities = [
        AUTHORITY_RANK[evidence_by_id[evidence_id].authority] / 100
        for tdoc in tdocs
        for evidence_id in tdoc.evidence_ids
        if evidence_id in evidence_by_id
    ]
    authority = max(authorities, default=0)
    final_status = max(
        (
            1.0
            if item.status in DECISION_STATUSES
            else 0.65
            if item.status in UNRESOLVED_STATUSES
            else 0.4
            for item in tdocs
        ),
        default=0,
    )
    companies = {company for item in tdocs for company in _companies(item)}
    cross_company = min(len(companies) / 4, 1)
    specification_impact = (
        1.0 if any(item.specifications or item.cr_number for item in tdocs) else 0
    )
    revision_component = min(max(revision_depth - 1, 0) / 4, 1)
    total = 100 * (
        0.25 * authority
        + 0.2 * final_status
        + 0.15 * revision_component
        + 0.1 * cross_company
        + 0.15 * specification_impact
        + 0.075 * novelty
        + 0.075 * persistence
    )
    return SignalScore(
        authority=authority,
        final_status=final_status,
        revision_depth=revision_component,
        cross_company=cross_company,
        specification_impact=specification_impact,
        novelty=novelty,
        persistence=persistence,
        total=round(total, 3),
    )


class NewsletterPacketBuilder:
    def __init__(self, config: NewsletterConfig | None = None) -> None:
        self.config = config or NewsletterConfig()

    def build(
        self,
        *,
        meeting: Meeting,
        dataset_version: str,
        comparison_meetings: list[Meeting],
        tdocs_by_meeting: dict[str, list[TDoc]],
        evidence: list[EvidenceRef],
        edition: Literal["provisional", "final"],
        provisional_packet: NewsletterPacket | None = None,
        meeting_briefing: MeetingBriefing | None = None,
        generated_at: datetime | None = None,
    ) -> NewsletterPacket:
        current = sorted(tdocs_by_meeting.get(meeting.id, []), key=lambda item: item.id)
        evidence_by_id = {item.id: item for item in evidence}
        ordered_meetings = sorted(
            comparison_meetings,
            key=lambda item: (item.ends_on or item.starts_on or date.min, item.number),
        )
        all_tdocs = [
            tdoc
            for selected_meeting in ordered_meetings
            for tdoc in tdocs_by_meeting.get(selected_meeting.id, [])
        ]
        by_id = {tdoc.id: tdoc for tdoc in all_tdocs}
        topic_groups: dict[str, list[TDoc]] = defaultdict(list)
        topic_by_meeting: dict[str, Counter[str]] = defaultdict(Counter)
        for tdoc in all_tdocs:
            key = _normalized_topic(tdoc)
            topic_groups[key].append(tdoc)
            topic_by_meeting[tdoc.meeting_id][key] += 1

        revision_analysis = self._revision_analysis(current, by_id)
        revision_depth = {
            identifier: analysis.depth
            for analysis in revision_analysis
            for identifier in analysis.chain
        }
        trends = self._topic_trends(ordered_meetings, topic_groups, topic_by_meeting)
        conclusion_changes = self._conclusion_changes(meeting, ordered_meetings, tdocs_by_meeting)
        status_groups = {
            status.value: [tdoc for tdoc in current if tdoc.status == status]
            for status in Conclusion
            if any(tdoc.status == status for tdoc in current)
        }
        company_counts = Counter(company for tdoc in current for company in _companies(tdoc))
        current_topic_counts = Counter(_normalized_topic(tdoc) for tdoc in current)
        technical_impacts = self._technical_impacts(current)

        signals = self._decision_signals(current, evidence_by_id, revision_depth)
        signals.extend(self._trend_signals(trends, topic_groups, evidence_by_id, revision_depth))
        signals.extend(
            self._impact_signals(technical_impacts, by_id, evidence_by_id, revision_depth)
        )
        signals.extend(self._revision_signals(revision_analysis, by_id, evidence_by_id))
        signals.extend(self._observation_signals(meeting_briefing))
        signals = self._deduplicate_and_rank(signals)
        implications, watch_items = self._implications_and_watch_items(
            trends, revision_analysis, technical_impacts, current, by_id, evidence_by_id
        )
        watch_items = self._deduplicate_and_rank(
            [
                *watch_items,
                *self._repeated_unsuccessful(topic_groups, evidence_by_id),
            ]
        )
        evidence_ids = _unique(
            [evidence_id for tdoc in current for evidence_id in tdoc.evidence_ids]
            + [
                evidence_id
                for item in [*signals, *implications, *watch_items]
                for evidence_id in item.evidence_ids
            ]
        )
        wanted_evidence = set(evidence_ids)
        evidence_catalog = [
            PacketEvidence(
                id=item.id,
                authority=item.authority,
                tdoc_id=item.tdoc_id,
                section_path=item.section_path,
                excerpt=(item.excerpt or "")[: self.config.evidence_excerpt_chars],
            )
            for item in sorted(evidence, key=lambda candidate: candidate.id)
            if item.id in wanted_evidence
        ]
        appendix = [
            TDocAppendixEntry(
                id=tdoc.id,
                title=tdoc.title,
                source=tdoc.source,
                agenda_item=tdoc.agenda_item,
                topic=tdoc.agenda_description,
                status=tdoc.status,
                revised_from=tdoc.revised_from,
                revised_to=tdoc.revised_to,
                specifications=tdoc.specifications,
                releases=tdoc.releases,
                work_items=tdoc.work_items,
                change_request=(
                    f"{tdoc.cr_number} rev {tdoc.cr_revision}"
                    if tdoc.cr_number and tdoc.cr_revision
                    else tdoc.cr_number
                ),
                evidence_ids=tdoc.evidence_ids,
            )
            for tdoc in current
        ]
        packet_data: dict[str, Any] = {
            "id": "pending",
            "packet_version": NEWSLETTER_PACKET_VERSION,
            "dataset_version": dataset_version,
            "meeting": meeting,
            "edition": edition,
            "generated_at": generated_at or datetime.now(UTC),
            "comparison_meetings": ordered_meetings,
            "comparison_window": len(ordered_meetings),
            "totals": {
                "tdocs": len(current),
                "meeting_observations": len(meeting_briefing.observations)
                if meeting_briefing
                else 0,
                **dict(Counter(t.status.value for t in current)),
            },
            "decisions": status_groups,
            "hot_topics": [
                {
                    "topic": _display_topic(topic_groups[key]),
                    "tdoc_count": count,
                    "evidence_ids": _unique(
                        [
                            eid
                            for item in topic_groups[key]
                            if item.meeting_id == meeting.id
                            for eid in item.evidence_ids
                        ]
                    ),
                }
                for key, count in current_topic_counts.most_common(20)
            ],
            "company_activity": [
                {
                    "company": company,
                    "tdoc_count": count,
                }
                for company, count in sorted(
                    company_counts.items(),
                    key=lambda item: (-item[1], item[0].casefold(), item[0]),
                )
            ],
            "revision_chains": [item.chain for item in revision_analysis],
            "affected_specs": [
                {
                    "specification": item.identifier,
                    "tdoc_count": len(item.tdoc_ids),
                    "evidence_ids": item.evidence_ids,
                }
                for item in technical_impacts
                if item.kind == "specification"
            ],
            "signals": signals,
            "topic_trends": trends,
            "revision_analysis": revision_analysis,
            "technical_impacts": technical_impacts,
            "conclusion_changes": conclusion_changes,
            "engineering_implications": implications,
            "watch_items": watch_items,
            "tdoc_appendix": appendix,
            "evidence_catalog": evidence_catalog,
            "evidence_ids": evidence_ids,
        }
        packet_data["provisional_to_final"] = self._delta(packet_data, provisional_packet)
        packet_data["id"] = self.packet_id(packet_data)
        return NewsletterPacket.model_validate(packet_data)

    def _observation_signals(
        self, briefing: MeetingBriefing | None
    ) -> list[NewsletterSignal]:
        if briefing is None:
            return []
        signals: list[NewsletterSignal] = []
        for item in briefing.observations:
            if not item.evidence_ids:
                continue
            authority = AUTHORITY_RANK[item.authority] / 100
            final_status = 1.0 if item.observation_type == "decision" else 0.7
            specification_impact = 1.0 if item.specification_ids else 0.0
            type_weight = {
                "decision": 1.0,
                "open_issue": 0.9,
                "follow_up_action": 0.85,
                "deadline": 0.85,
                "dependency": 0.75,
                "intended_outcome": 0.65,
                "discussion_summary": 0.55,
            }[item.observation_type]
            total = 100 * (
                0.4 * authority
                + 0.25 * final_status
                + 0.15 * specification_impact
                + 0.2 * type_weight
            )
            context = item.tdoc_ids[0] if item.tdoc_ids else item.agenda_item or "meeting"
            signals.append(
                NewsletterSignal(
                    id=_signal_id(
                        f"meeting_{item.observation_type}",
                        item.content_hash,
                        item.tdoc_ids,
                    ),
                    category=f"meeting_{item.observation_type}",
                    headline=f"{_observation_label(item)}: {context}",
                    detail=item.text,
                    tdoc_ids=item.tdoc_ids,
                    evidence_ids=item.evidence_ids,
                    score=SignalScore(
                        authority=authority,
                        final_status=final_status,
                        revision_depth=0,
                        cross_company=0,
                        specification_impact=specification_impact,
                        novelty=type_weight,
                        persistence=0,
                        total=round(total, 3),
                    ),
                )
            )
        return signals

    @staticmethod
    def packet_id(packet_data: dict[str, Any]) -> str:
        canonical = dict(packet_data)
        canonical.pop("id", None)
        canonical.pop("generated_at", None)
        serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(serialized.encode()).hexdigest()[:20]
        meeting = packet_data["meeting"]
        meeting_id = meeting.id if isinstance(meeting, Meeting) else meeting["id"]
        return f"newsletter-{meeting_id}-{packet_data['edition']}-{digest}"

    def _revision_analysis(
        self, current: list[TDoc], by_id: dict[str, TDoc]
    ) -> list[RevisionAnalysis]:
        results: dict[tuple[str, ...], RevisionAnalysis] = {}
        for tdoc in current:
            cursor = tdoc
            seen: set[str] = set()
            while cursor.revised_from and cursor.revised_from in by_id and cursor.id not in seen:
                seen.add(cursor.id)
                cursor = by_id[cursor.revised_from]
            chain: list[TDoc] = []
            seen.clear()
            while cursor.id not in seen:
                seen.add(cursor.id)
                chain.append(cursor)
                if not cursor.revised_to or cursor.revised_to not in by_id:
                    break
                cursor = by_id[cursor.revised_to]
            if any(item.meeting_id == tdoc.meeting_id for item in chain) and len(chain) > 1:
                key = tuple(item.id for item in chain)
                results[key] = RevisionAnalysis(
                    chain=list(key),
                    depth=len(chain),
                    meeting_ids=_unique([item.meeting_id for item in chain]),
                    final_status=chain[-1].status,
                    evidence_ids=_unique([eid for item in chain for eid in item.evidence_ids]),
                )
        return sorted(results.values(), key=lambda item: (-item.depth, item.chain))

    def _topic_trends(
        self,
        meetings: list[Meeting],
        topic_groups: dict[str, list[TDoc]],
        topic_by_meeting: dict[str, Counter[str]],
    ) -> list[TopicTrend]:
        if not meetings:
            return []
        current_id = meetings[-1].id
        results: list[TopicTrend] = []
        for key, selected in topic_groups.items():
            counts = {item.id: topic_by_meeting[item.id][key] for item in meetings}
            values = list(counts.values())
            current_count = counts[current_id]
            prior = values[:-1]
            current_docs = [item for item in selected if item.meeting_id == current_id]
            statuses = sorted({item.status for item in current_docs}, key=lambda item: item.value)
            positive = any(status in POSITIVE_STATUSES for status in statuses)
            negative = any(status in NEGATIVE_STATUSES for status in statuses)
            if current_count and not any(prior):
                classification = "new"
            elif current_count == 0 and any(prior):
                classification = "declining"
            elif prior and current_count > max(prior) and current_count >= 2:
                classification = "accelerating"
            elif positive and negative:
                classification = "contested"
            elif sum(value > 0 for value in values) >= min(3, len(values)):
                classification = "persistent"
            else:
                classification = "stable"
            results.append(
                TopicTrend(
                    topic=_display_topic(selected),
                    classification=cast(Any, classification),
                    counts_by_meeting=counts,
                    statuses=statuses,
                    companies=sorted(
                        {company for item in current_docs for company in _companies(item)},
                        key=lambda item: (item.casefold(), item),
                    ),
                    tdoc_ids=[item.id for item in current_docs],
                    evidence_ids=_unique([eid for item in selected for eid in item.evidence_ids]),
                )
            )
        rank = {
            "contested": 0,
            "accelerating": 1,
            "new": 2,
            "persistent": 3,
            "declining": 4,
            "stable": 5,
        }
        return sorted(results, key=lambda item: (rank[item.classification], item.topic.casefold()))

    @staticmethod
    def _technical_impacts(current: list[TDoc]) -> list[TechnicalImpact]:
        values: dict[tuple[str, str], list[TDoc]] = defaultdict(list)
        for tdoc in current:
            for identifier in tdoc.specifications:
                values[("specification", identifier)].append(tdoc)
            for identifier in tdoc.releases:
                values[("release", identifier)].append(tdoc)
            for identifier in tdoc.work_items:
                values[("work_item", identifier)].append(tdoc)
            if tdoc.cr_number:
                values[("change_request", tdoc.cr_number)].append(tdoc)
        return [
            TechnicalImpact(
                kind=cast(Any, kind),
                identifier=identifier,
                tdoc_ids=sorted(item.id for item in selected),
                evidence_ids=_unique([eid for item in selected for eid in item.evidence_ids]),
            )
            for (kind, identifier), selected in sorted(values.items())
        ]

    def _decision_signals(
        self,
        current: list[TDoc],
        evidence_by_id: dict[str, EvidenceRef],
        revision_depth: dict[str, int],
    ) -> list[NewsletterSignal]:
        groups: dict[tuple[Conclusion, str], list[TDoc]] = defaultdict(list)
        for tdoc in current:
            if tdoc.status in DECISION_STATUSES:
                groups[(tdoc.status, _normalized_topic(tdoc))].append(tdoc)
        results: list[NewsletterSignal] = []
        for (status, key), selected in groups.items():
            evidence_ids = _unique([eid for item in selected for eid in item.evidence_ids])
            if not evidence_ids:
                continue
            topic = _display_topic(selected)
            identifiers = [item.id for item in selected]
            results.append(
                NewsletterSignal(
                    id=_signal_id("decision", f"{status.value}:{key}", identifiers),
                    category="decision",
                    headline=f"{topic}: {status.value.replace('_', ' ')}",
                    detail=(
                        f"{len(selected)} TDoc(s) concluded as {status.value.replace('_', ' ')}: "
                        f"{', '.join(identifiers)}."
                    ),
                    tdoc_ids=identifiers,
                    evidence_ids=evidence_ids,
                    score=_score(
                        selected,
                        evidence_by_id=evidence_by_id,
                        revision_depth=max(revision_depth.get(item.id, 1) for item in selected),
                    ),
                )
            )
        return results

    def _trend_signals(
        self,
        trends: list[TopicTrend],
        topic_groups: dict[str, list[TDoc]],
        evidence_by_id: dict[str, EvidenceRef],
        revision_depth: dict[str, int],
    ) -> list[NewsletterSignal]:
        results: list[NewsletterSignal] = []
        by_display = {_display_topic(items): items for items in topic_groups.values()}
        for trend in trends:
            if trend.classification == "stable" or not trend.evidence_ids:
                continue
            selected = by_display[trend.topic]
            results.append(
                NewsletterSignal(
                    id=_signal_id(
                        "topic_trend",
                        f"{trend.classification}:{trend.topic}",
                        trend.tdoc_ids,
                    ),
                    category="topic_trend",
                    headline=f"{trend.topic}: {trend.classification}",
                    detail=(
                        "Meeting counts across the comparison window are "
                        + ", ".join(
                            f"{key}={value}" for key, value in trend.counts_by_meeting.items()
                        )
                        + "."
                    ),
                    tdoc_ids=trend.tdoc_ids,
                    evidence_ids=trend.evidence_ids,
                    score=_score(
                        selected,
                        evidence_by_id=evidence_by_id,
                        revision_depth=max(
                            (revision_depth.get(item.id, 1) for item in selected), default=1
                        ),
                        novelty=1 if trend.classification == "new" else 0,
                        persistence=(
                            sum(value > 0 for value in trend.counts_by_meeting.values())
                            / len(trend.counts_by_meeting)
                        ),
                    ),
                )
            )
        return results

    def _impact_signals(
        self,
        impacts: list[TechnicalImpact],
        by_id: dict[str, TDoc],
        evidence_by_id: dict[str, EvidenceRef],
        revision_depth: dict[str, int],
    ) -> list[NewsletterSignal]:
        results: list[NewsletterSignal] = []
        for impact in impacts:
            selected = [by_id[item] for item in impact.tdoc_ids]
            if not impact.evidence_ids:
                continue
            results.append(
                NewsletterSignal(
                    id=_signal_id("technical_impact", impact.identifier, impact.tdoc_ids),
                    category="technical_impact",
                    headline=f"{impact.kind.replace('_', ' ').title()}: {impact.identifier}",
                    detail=(
                        f"Referenced by {len(impact.tdoc_ids)} TDoc(s): "
                        f"{', '.join(impact.tdoc_ids)}."
                    ),
                    tdoc_ids=impact.tdoc_ids,
                    evidence_ids=impact.evidence_ids,
                    score=_score(
                        selected,
                        evidence_by_id=evidence_by_id,
                        revision_depth=max(revision_depth.get(item.id, 1) for item in selected),
                    ),
                )
            )
        return results

    def _revision_signals(
        self,
        analyses: list[RevisionAnalysis],
        by_id: dict[str, TDoc],
        evidence_by_id: dict[str, EvidenceRef],
    ) -> list[NewsletterSignal]:
        results = []
        for item in analyses:
            selected = [by_id[identifier] for identifier in item.chain]
            if not item.evidence_ids:
                continue
            results.append(
                NewsletterSignal(
                    id=_signal_id("revision", item.chain[-1], item.chain),
                    category="revision",
                    headline=f"Revision chain reached depth {item.depth}",
                    detail=f"{' -> '.join(item.chain)}; final status {item.final_status.value}.",
                    tdoc_ids=item.chain,
                    evidence_ids=item.evidence_ids,
                    score=_score(
                        selected,
                        evidence_by_id=evidence_by_id,
                        revision_depth=item.depth,
                        persistence=min(len(item.meeting_ids) / 3, 1),
                    ),
                )
            )
        return results

    def _implications_and_watch_items(
        self,
        trends: list[TopicTrend],
        analyses: list[RevisionAnalysis],
        impacts: list[TechnicalImpact],
        current: list[TDoc],
        by_id: dict[str, TDoc],
        evidence_by_id: dict[str, EvidenceRef],
    ) -> tuple[list[NewsletterSignal], list[NewsletterSignal]]:
        implications: list[NewsletterSignal] = []
        watch: list[NewsletterSignal] = []
        for item in analyses:
            if item.depth < 3 or not item.evidence_ids:
                continue
            selected = [by_id[identifier] for identifier in item.chain]
            implications.append(
                NewsletterSignal(
                    id=_signal_id("implication", "revision_churn", item.chain),
                    category="revision_churn",
                    headline="Repeated revision warrants architectural review",
                    detail=(
                        f"Chain {' -> '.join(item.chain)} reached depth {item.depth}; "
                        "the underlying issue may still contain sensitive trade-offs."
                    ),
                    tdoc_ids=item.chain,
                    evidence_ids=item.evidence_ids,
                    score=_score(
                        selected,
                        evidence_by_id=evidence_by_id,
                        revision_depth=item.depth,
                        persistence=min(len(item.meeting_ids) / 3, 1),
                    ),
                    fact_or_inference="engineering_implication",
                )
            )
        for trend in trends:
            if trend.classification not in {"contested", "accelerating"} or not trend.evidence_ids:
                continue
            selected = [by_id[item] for item in trend.tdoc_ids if item in by_id]
            if not selected:
                continue
            implications.append(
                NewsletterSignal(
                    id=_signal_id("implication", trend.classification, trend.tdoc_ids),
                    category="topic_attention",
                    headline=f"Monitor {trend.topic}",
                    detail=(
                        f"The topic is {trend.classification}; architects should review the cited "
                        "proposals and conclusions before relying on a stable direction."
                    ),
                    tdoc_ids=trend.tdoc_ids,
                    evidence_ids=trend.evidence_ids,
                    score=_score(selected, evidence_by_id=evidence_by_id, persistence=1),
                    fact_or_inference="engineering_implication",
                )
            )
        for tdoc in current:
            if tdoc.status not in UNRESOLVED_STATUSES or not tdoc.evidence_ids:
                continue
            watch.append(
                NewsletterSignal(
                    id=_signal_id("watch", tdoc.status.value, [tdoc.id]),
                    category="unresolved",
                    headline=f"{tdoc.id} remains {tdoc.status.value.replace('_', ' ')}",
                    detail=tdoc.title,
                    tdoc_ids=[tdoc.id],
                    evidence_ids=tdoc.evidence_ids,
                    score=_score([tdoc], evidence_by_id=evidence_by_id),
                )
            )
        for impact in impacts:
            if impact.kind != "specification" or not impact.evidence_ids:
                continue
            selected = [by_id[item] for item in impact.tdoc_ids]
            if not any(item.status in POSITIVE_STATUSES for item in selected):
                continue
            implications.append(
                NewsletterSignal(
                    id=_signal_id("implication", impact.identifier, impact.tdoc_ids),
                    category="specification_review",
                    headline=f"Review impact on {impact.identifier}",
                    detail=(
                        f"Agreed or approved TDocs reference {impact.identifier}; downstream "
                        "implementation planning should verify the cited changes."
                    ),
                    tdoc_ids=impact.tdoc_ids,
                    evidence_ids=impact.evidence_ids,
                    score=_score(selected, evidence_by_id=evidence_by_id),
                    fact_or_inference="engineering_implication",
                )
            )
        return self._deduplicate_and_rank(implications), self._deduplicate_and_rank(watch)

    def _repeated_unsuccessful(
        self,
        topic_groups: dict[str, list[TDoc]],
        evidence_by_id: dict[str, EvidenceRef],
    ) -> list[NewsletterSignal]:
        results = []
        for selected in topic_groups.values():
            unsuccessful = [item for item in selected if item.status in NEGATIVE_STATUSES]
            meeting_ids = {item.meeting_id for item in unsuccessful}
            evidence_ids = _unique([eid for item in unsuccessful for eid in item.evidence_ids])
            if len(meeting_ids) < 2 or not evidence_ids:
                continue
            tdoc_ids = [item.id for item in unsuccessful]
            results.append(
                NewsletterSignal(
                    id=_signal_id("watch", "repeated_unsuccessful", tdoc_ids),
                    category="repeated_unsuccessful",
                    headline=f"Repeated unsuccessful proposals: {_display_topic(selected)}",
                    detail=(
                        f"{len(unsuccessful)} rejected, not-pursued, postponed, or withdrawn "
                        f"TDocs span {len(meeting_ids)} meetings."
                    ),
                    tdoc_ids=tdoc_ids,
                    evidence_ids=evidence_ids,
                    score=_score(
                        unsuccessful,
                        evidence_by_id=evidence_by_id,
                        persistence=min(len(meeting_ids) / 3, 1),
                    ),
                )
            )
        return results

    @staticmethod
    def _conclusion_changes(
        current: Meeting,
        meetings: list[Meeting],
        tdocs_by_meeting: dict[str, list[TDoc]],
    ) -> list[dict[str, Any]]:
        prior = [item for item in meetings if item.id != current.id]
        if not prior:
            return []
        previous = prior[-1]
        old: dict[str, set[str]] = defaultdict(set)
        new: dict[str, set[str]] = defaultdict(set)
        labels: dict[str, str] = {}
        for tdoc in tdocs_by_meeting.get(previous.id, []):
            key = _normalized_topic(tdoc)
            old[key].add(tdoc.status.value)
            labels[key] = tdoc.agenda_description or tdoc.title
        for tdoc in tdocs_by_meeting.get(current.id, []):
            key = _normalized_topic(tdoc)
            new[key].add(tdoc.status.value)
            labels[key] = tdoc.agenda_description or tdoc.title
        return [
            {
                "topic": labels[key],
                "previous_meeting": previous.id,
                "previous_statuses": sorted(old[key]),
                "current_statuses": sorted(new[key]),
            }
            for key in sorted(old.keys() & new.keys())
            if old[key] != new[key]
        ]

    def _deduplicate_and_rank(self, signals: list[NewsletterSignal]) -> list[NewsletterSignal]:
        unique = {item.id: item for item in signals}
        return sorted(unique.values(), key=lambda item: (-item.score.total, item.id))[
            : self.config.max_signal_items
        ]

    @staticmethod
    def _delta(
        packet_data: dict[str, Any], provisional: NewsletterPacket | None
    ) -> NewsletterDelta | None:
        if packet_data["edition"] != "final" or provisional is None:
            return None
        current = {item.id: item for item in packet_data["signals"]}
        old = {item.id: item for item in provisional.signals}
        return NewsletterDelta(
            provisional_packet_id=provisional.id,
            added_signal_ids=sorted(current.keys() - old.keys()),
            removed_signal_ids=sorted(old.keys() - current.keys()),
            changed_conclusions=[
                {
                    "topic": str(item["topic"]),
                    "from": ", ".join(item["previous_statuses"]),
                    "to": ", ".join(item["current_statuses"]),
                }
                for item in packet_data["conclusion_changes"]
            ],
        )


class RenderedParagraph(BaseModel):
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    organizations: list[str] = Field(default_factory=list)
    specifications: list[str] = Field(default_factory=list)
    conclusions: list[Conclusion] = Field(default_factory=list)


class RenderedSection(BaseModel):
    kind: Literal[
        "material_changes",
        "decisions",
        "topic_evolution",
        "technical_impact",
        "company_activity",
        "engineering_implications",
        "watch_items",
        "appendix_summary",
    ]
    title: str
    paragraphs: list[RenderedParagraph] = Field(default_factory=list)


class RenderedNewsletter(BaseModel):
    title: str
    executive_summary: list[RenderedParagraph] = Field(min_length=1)
    sections: list[RenderedSection] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def complete_sections(self) -> RenderedNewsletter:
        kinds = [section.kind for section in self.sections]
        if len(set(kinds)) != len(kinds) or set(kinds) != REQUIRED_RENDERED_SECTIONS:
            raise ValueError("newsletter must contain each required section exactly once")
        return self


class NewsletterRenderer:
    def __init__(
        self,
        features: FeatureConfig,
        client: OpenAICompatibleClient | None,
        config: NewsletterConfig | None = None,
    ) -> None:
        self.features = features
        self.client = client
        self.config = config or NewsletterConfig()

    async def render(
        self, packet_envelope: Envelope[NewsletterPacket | None]
    ) -> Envelope[RenderedNewsletter | None]:
        if packet_envelope.data is None:
            return Envelope(
                data=None,
                dataset_version=packet_envelope.dataset_version,
                completeness="unavailable",
                confidence=0,
                warnings=packet_envelope.warnings,
            )
        if packet_envelope.completeness != "complete":
            raise ValueError("newsletter publication requires a complete briefing packet")
        if not self.features.newsletter_generation_enabled or not self.client:
            return Envelope(
                data=None,
                evidence=packet_envelope.evidence,
                dataset_version=packet_envelope.dataset_version,
                completeness="unavailable",
                confidence=0,
                warnings=["Newsletter prose generation is disabled; use the briefing packet"],
            )
        payload = self._render_payload(packet_envelope.data)
        schema = RenderedNewsletter.model_json_schema()
        raw = await self.client.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        f"3GPP analytical newsletter prompt {NEWSLETTER_PROMPT_VERSION}. "
                        "Use only the supplied packet. Do not infer additional facts. Every "
                        "paragraph must cite supporting evidence IDs and declare every "
                        "organization, "
                        "specification, and conclusion named in its text. Keep company positions "
                        "neutral. Engineering implications must remain in their dedicated section."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, sort_keys=True)},
            ],
            schema_name="threegpp_wg_newsletter_v1",
            schema=schema,
        )
        rendered = RenderedNewsletter.model_validate(raw)
        self._validate(rendered, packet_envelope.data, payload)
        return Envelope(
            data=rendered,
            evidence=packet_envelope.evidence,
            dataset_version=packet_envelope.dataset_version,
        )

    def _render_payload(self, packet: NewsletterPacket) -> dict[str, Any]:
        signals = [*packet.signals, *packet.engineering_implications, *packet.watch_items]
        signals = sorted(signals, key=lambda item: (-item.score.total, item.id))[
            : self.config.max_signal_items
        ]
        needed = {evidence_id for item in signals for evidence_id in item.evidence_ids}
        evidence = [
            item.model_dump(mode="json") for item in packet.evidence_catalog if item.id in needed
        ][: self.config.max_render_evidence_items]
        allowed_evidence = {item["id"] for item in evidence}
        return {
            "packet_id": packet.id,
            "packet_version": packet.packet_version,
            "dataset_version": packet.dataset_version,
            "meeting": packet.meeting.model_dump(mode="json"),
            "edition": packet.edition,
            "comparison_meetings": [item.id for item in packet.comparison_meetings],
            "totals": packet.totals,
            "signals": [
                item.model_dump(mode="json")
                for item in signals
                if set(item.evidence_ids) & allowed_evidence
            ],
            "topic_trends": [item.model_dump(mode="json") for item in packet.topic_trends],
            "company_activity": packet.company_activity,
            "technical_impacts": [
                item.model_dump(mode="json") for item in packet.technical_impacts
            ],
            "conclusion_changes": packet.conclusion_changes,
            "provisional_to_final": (
                packet.provisional_to_final.model_dump(mode="json")
                if packet.provisional_to_final
                else None
            ),
            "appendix_totals": {"tdocs": len(packet.tdoc_appendix)},
            "evidence": evidence,
        }

    @staticmethod
    def _paragraphs(rendered: RenderedNewsletter) -> list[RenderedParagraph]:
        return [
            *rendered.executive_summary,
            *(paragraph for section in rendered.sections for paragraph in section.paragraphs),
        ]

    def _validate(
        self,
        rendered: RenderedNewsletter,
        packet: NewsletterPacket,
        payload: dict[str, Any],
    ) -> None:
        paragraphs = self._paragraphs(rendered)
        allowed_evidence = {item["id"] for item in payload["evidence"]}
        invalid_evidence = {
            evidence_id
            for paragraph in paragraphs
            for evidence_id in paragraph.evidence_ids
            if evidence_id not in allowed_evidence
        }
        if invalid_evidence:
            raise ValueError(
                f"newsletter contains unsupported evidence IDs: {sorted(invalid_evidence)}"
            )
        packet_text = json.dumps(payload, sort_keys=True)
        allowed_numbers = set(re.findall(r"\b\d+(?:\.\d+)*\b", packet_text))
        unsupported_numbers = {
            value
            for paragraph in paragraphs
            for value in re.findall(r"\b\d+(?:\.\d+)*\b", paragraph.text)
            if value not in allowed_numbers
        }
        if unsupported_numbers:
            raise ValueError(
                f"newsletter contains unsupported numbers: {sorted(unsupported_numbers)}"
            )
        allowed_organizations = {str(item["company"]) for item in packet.company_activity}
        allowed_specs = {
            item.identifier for item in packet.technical_impacts if item.kind == "specification"
        }
        allowed_conclusions = {item.status for item in packet.tdoc_appendix}
        for paragraph in paragraphs:
            unsupported_orgs = set(paragraph.organizations) - allowed_organizations
            undeclared_orgs = {
                item
                for item in allowed_organizations
                if re.search(rf"(?<!\w){re.escape(item)}(?!\w)", paragraph.text, re.IGNORECASE)
                and item not in paragraph.organizations
            }
            missing_org_text = any(
                item.casefold() not in paragraph.text.casefold() for item in paragraph.organizations
            )
            if unsupported_orgs or undeclared_orgs or missing_org_text:
                raise ValueError("newsletter contains unsupported organization attribution")
            unsupported_specs = set(paragraph.specifications) - allowed_specs
            undeclared_specs = {
                item
                for item in allowed_specs
                if item in paragraph.text and item not in paragraph.specifications
            }
            if unsupported_specs or undeclared_specs:
                raise ValueError("newsletter contains unsupported specification attribution")
            if set(paragraph.conclusions) - allowed_conclusions:
                raise ValueError("newsletter contains unsupported conclusion attribution")
            named_conclusions = {
                conclusion
                for conclusion in allowed_conclusions
                if re.search(
                    rf"(?<!\w){re.escape(conclusion.value.replace('_', ' '))}(?!\w)",
                    paragraph.text,
                    re.IGNORECASE,
                )
            }
            if named_conclusions - set(paragraph.conclusions) or any(
                conclusion.value.replace("_", " ").casefold() not in paragraph.text.casefold()
                for conclusion in paragraph.conclusions
            ):
                raise ValueError("newsletter contains undeclared conclusion attribution")
