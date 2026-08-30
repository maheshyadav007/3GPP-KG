from __future__ import annotations

from datetime import date

from .constants import BlockKind, Conclusion, EvidenceAuthority
from .domain import DocumentBlock, EvidenceRef, Meeting, RetrievalChunk, TDoc
from .repository import InMemoryRepository


def demo_repository() -> InMemoryRepository:
    meetings = [
        Meeting(
            id="RAN2-133",
            working_group_id="RAN2",
            number=133,
            name="RAN2 #133",
            source_url="https://www.3gpp.org/ftp/tsg_ran/WG2_RL2/TSGR2_133/",
            starts_on=date(2026, 5, 4),
            ends_on=date(2026, 5, 8),
            readiness="final_ready",
        ),
        Meeting(
            id="SA2-175",
            working_group_id="SA2",
            number=175,
            name="SA2 #175",
            source_url="https://www.3gpp.org/ftp/tsg_sa/WG2_Arch/TSGS2_175_Dalian_2026-05/",
            starts_on=date(2026, 5, 18),
            ends_on=date(2026, 5, 22),
            readiness="provisional_ready",
        ),
    ]
    evidence = [
        EvidenceRef(
            id="ev-r2-report-658",
            source_url="https://www.3gpp.org/ftp/tsg_ran/WG2_RL2/TSGR2_133/Report/R2-2601402.zip",
            artifact_sha256="8e5ec15e7bbce1ea225d8b17d69fbb38ad21c6b0a2e8af23e4b4a0ecf778a001",
            authority=EvidenceAuthority.FINAL_REPORT,
            meeting_id="RAN2-133",
            tdoc_id="R2-2601389",
            section_path=["Carrier aggregation"],
            block_id="report-block-658",
            excerpt="All three documents are agreed.",
        ),
        EvidenceRef(
            id="ev-sa2-index-1522",
            source_url="https://www.3gpp.org/ftp/tsg_sa/WG2_Arch/TSGS2_175_Dalian_2026-05/SA2-175_Index_2026.zip",
            artifact_sha256="213ba7271ad19cc87f909ef4a8675d8e99055510779768339989c64c66ddbf21",
            authority=EvidenceAuthority.MEETING_EXPORT,
            meeting_id="SA2-175",
            tdoc_id="S2-2604823",
            section_path=["20.2.2", "Sensing architecture"],
            block_id="sheet-row-1522",
            excerpt="Revised in parallel session to S2-2605162.",
        ),
    ]
    tdocs = [
        TDoc(
            id="R2-2600134",
            meeting_id="RAN2-133",
            title="Clarification on simultaneous PUCCH and PUSCH transmission in CA",
            source="Qualcomm, Ericsson",
            document_type="CR",
            purpose="Agreement",
            agenda_item="8.7",
            agenda_description="Carrier aggregation",
            status=Conclusion.REVISED,
            status_raw="revised",
            revised_to="R2-2601273",
            releases=["Rel-20"],
            specifications=["38.306"],
            work_items=["NR_newRAT-Core"],
            cr_number="1401",
            evidence_ids=["ev-r2-report-658"],
        ),
        TDoc(
            id="R2-2601273",
            meeting_id="RAN2-133",
            title="Clarification on simultaneous PUCCH and PUSCH transmission in CA",
            source="Qualcomm, Ericsson",
            document_type="CR",
            purpose="Agreement",
            agenda_item="8.7",
            agenda_description="Carrier aggregation",
            status=Conclusion.REVISED,
            status_raw="revised",
            revised_from="R2-2600134",
            revised_to="R2-2601389",
            releases=["Rel-20"],
            specifications=["38.306"],
            work_items=["NR_newRAT-Core"],
            cr_number="1401",
            evidence_ids=["ev-r2-report-658"],
        ),
        TDoc(
            id="R2-2601389",
            meeting_id="RAN2-133",
            title="Clarification on simultaneous PUCCH and PUSCH transmission in CA",
            source="Qualcomm, Ericsson",
            document_type="CR",
            purpose="Agreement",
            agenda_item="8.7",
            agenda_description="Carrier aggregation",
            status=Conclusion.AGREED,
            status_raw="agreed",
            revised_from="R2-2601273",
            releases=["Rel-20"],
            specifications=["38.306"],
            work_items=["NR_newRAT-Core"],
            cr_number="1401",
            evidence_ids=["ev-r2-report-658"],
        ),
        TDoc(
            id="S2-2604823",
            meeting_id="SA2-175",
            title="Enhancements to support sensing services",
            source="Xiaomi, vivo, OPPO, ZTE",
            document_type="CR",
            purpose="Approval",
            agenda_item="20.2.2",
            agenda_description="Sensing architecture",
            status=Conclusion.REVISED,
            status_raw="Revised",
            revised_to="S2-2605162",
            releases=["Rel-20"],
            specifications=["23.501"],
            work_items=["Sensing-ARC"],
            cr_number="6597",
            cr_revision="1",
            cr_category="B",
            evidence_ids=["ev-sa2-index-1522"],
        ),
        TDoc(
            id="S2-2605162",
            meeting_id="SA2-175",
            title="Enhancements to support sensing services",
            source="Xiaomi, vivo, OPPO, ZTE",
            document_type="CR",
            purpose="Approval",
            agenda_item="20.2.2",
            agenda_description="Sensing architecture",
            status=Conclusion.AGREED,
            status_raw="Agreed",
            revised_from="S2-2604823",
            releases=["Rel-20"],
            specifications=["23.501"],
            work_items=["Sensing-ARC"],
            cr_number="6597",
            cr_revision="2",
            cr_category="B",
            evidence_ids=["ev-sa2-index-1522"],
        ),
    ]
    chunks = [
        RetrievalChunk(
            id="chunk-r2-ca-conclusion",
            document_id="R2-2601389",
            block_ids=["report-block-658"],
            text=(
                "The group agreed the clarification for simultaneous PUCCH and PUSCH "
                "transmission in carrier aggregation."
            ),
            section_path=["Carrier aggregation", "Conclusion"],
            token_count=18,
            evidence_ids=["ev-r2-report-658"],
        ),
        RetrievalChunk(
            id="chunk-sa2-sensing",
            document_id="S2-2605162",
            block_ids=["sheet-row-1522"],
            text="The sensing architecture CR was revised and agreed as S2-2605162.",
            section_path=["20.2.2", "Sensing architecture"],
            token_count=13,
            evidence_ids=["ev-sa2-index-1522"],
        ),
    ]
    blocks = [
        DocumentBlock(
            id="report-block-658",
            document_id="R2-2601389",
            index=0,
            kind=BlockKind.CONCLUSION,
            text=(
                "The group agreed the clarification for simultaneous PUCCH and PUSCH "
                "transmission in carrier aggregation."
            ),
            section_path=["Carrier aggregation", "Conclusion"],
        ),
        DocumentBlock(
            id="sheet-row-1522",
            document_id="S2-2605162",
            index=0,
            kind=BlockKind.CONCLUSION,
            text="The sensing architecture CR was revised and agreed as S2-2605162.",
            section_path=["20.2.2", "Sensing architecture"],
        ),
    ]
    return InMemoryRepository(meetings, tdocs, evidence, chunks, blocks)
