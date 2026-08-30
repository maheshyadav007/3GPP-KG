# 3GPP Meeting And Evidence Source Model

## Organizational hierarchy

The canonical hierarchy is 3GPP, Technical Specification Group (TSG), Working Group (WG),
meeting, agenda item, and temporary document (TDoc). RAN2 and RAN3 belong to TSG RAN; SA2
belongs to TSG SA. Meetings include regular, `bis`, electronic (`e`), ad-hoc (`AH`), and joint
variants. The original directory name is retained because numbering alone is not globally unique.

## Meeting artifacts

Recent RAN2 meetings commonly expose `Agenda`, `Docs`, `Inbox`, `Invitation`, `LSin`, `LSout`,
and `Report`. RAN3 adds a separate `TdocList` directory. SA2 commonly exposes `Agenda`, `Docs`,
`INBOX`, `Report`, `Templates`, an index workbook, and generated `TdocsByAgenda` pages. Historical
meetings vary in capitalization and may use `Documents` instead of `Docs`.

The meeting spreadsheet is the best inventory of what exists. Important fields include TDoc,
title, source, type, purpose, agenda item, status, upload date, revision links, release,
specification, work item, CR metadata, clauses affected, and liaison relationships. SA2 index
workbooks can additionally contain summary, discussion, and conclusion fields, but also contain
registration and administrative sheets that must not be ingested.

## Document and report lifecycle

A TDoc number identifies a submitted document. A revision normally receives a new TDoc number;
`is revision of` and `revised to` fields create a directed chain. Merges and liaison replies are
separate relationships. A file at one URL can also be replaced, so every observed binary is an
immutable `ArtifactVersion` identified by SHA-256.

Meeting reports have their own lifecycle. A draft may be created at the end of meeting N, then be
revised or approved as a TDoc submitted at meeting N+1. Therefore reports have both `submitted_at`
and `reports_on` relationships. Provisional and approved reports must not be conflated.

## Conclusions and authority

Canonical conclusions include available, agreed, approved, endorsed, merged, noted, not pursued,
not treated, postponed, reissued, rejected, revised, reserved, and withdrawn. The original source
value is always retained alongside the canonical value.

Conflicting facts are not overwritten. They remain source-qualified and are ranked:

1. Approved report or portal record.
2. Final meeting report.
3. Draft meeting report or final meeting spreadsheet.
4. TDoc body and cover sheet.
5. Model-derived topic or relationship.

Every returned fact must include an evidence reference. Model-derived values are marked as inferred
and never silently promoted to official 3GPP conclusions.

## Parsing and privacy boundaries

DOCX is parsed as OOXML so headings, paragraphs, tables, styles, and tracked-change state can be
preserved. Legacy DOC is converted in an isolated worker. PDF is a fallback source. ZIP archives
are validated against member-count, path-traversal, and expanded-size limits before extraction.
Macros and external Office relationships are never executed.

Registration sheets, voting lists, email addresses, contact IDs, and workbook administration data
are excluded. Submitter organizations are retained; individual contacts are not graph entities.

## Validation corpus

The directory contract was checked against the official 3GPP listings on 2026-08-29. The live
validation set is configuration-driven and contains three recent completed meetings plus one older
meeting per WG:

| WG | Recent samples | Older sample | Observed structure |
| --- | --- | --- | --- |
| RAN2 | 130, 131, 132 | 120 | `Agenda`, `Docs`, `Report`; TDoc list in `Docs` |
| RAN3 | 130, 131, 132 | 120 | `Agenda`, `Docs`, `Report`, separate `TdocList` |
| SA2 | 170, 171, 172 | 160 | root index archive/pages plus `Agenda`, `Docs`, `Report` |

The check discovered 189 matching RAN2 directories, 179 RAN3 directories, and 168 canonical SA2
meetings. SA2 location/date suffixes are source labels rather than meeting variants; `AH-e`, `bis`,
and electronic suffixes remain variants. RAN listings contain historical forms such as `bis-e`,
`_e`, and `-e`, which are normalized while preserving the original directory name.

Run `threegpp-kg validate-sources --output <proof.json>` to repeat the live structure check. This
validates directory and artifact discovery, not spreadsheet row fidelity or document extraction;
those require downloaded golden fixtures and reconciliation tests before production release.
