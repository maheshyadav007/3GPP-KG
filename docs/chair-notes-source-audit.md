# Chair Notes Source Audit

Audit date: 2026-09-02

## Sample

- Working group meeting: RAN2#132 (`TSGR2_132`)
- Chair notes source: <https://www.3gpp.org/ftp/tsg_ran/WG2_RL2/TSGR2_132/Inbox/Chair_Notes/R2_132_ChairNotes_11-21_final_clean.zip>
- Final report source: <https://www.3gpp.org/ftp/tsg_ran/WG2_RL2/TSGR2_132/Report/R2-2600002.zip>
- Post-meeting discussion source: <https://www.3gpp.org/ftp/tsg_ran/WG2_RL2/TSGR2_132/Inbox/Email_Discussions/RAN2_132%20Post%20email%20discussions%20v00.docx>
- Local sample directory: `data/research/chairnotes/TSGR2_132/`
- Chair notes ZIP SHA-256: `ac5eb1b64e66b16d9f188341052de0929463b6f73a152550b2f850c6485eaec0`

## Parser Results

The existing document parser successfully parsed all three DOCX artifacts.

| Artifact | Blocks | Words | TDoc references | Discussion blocks | Agreement blocks |
| --- | ---: | ---: | ---: | ---: | ---: |
| Final chair notes | 3,785 | 57,660 | 1,269 | 270 | 282 |
| Final report | 7,377 | 115,196 | 1,516 | 774 | 864 |
| Post-meeting email discussions | 180 | 1,466 | 25 | 0 | 0 |

The chair notes contain the agenda hierarchy, TDoc metadata, treatment order, chair conclusions,
agreements, rejected or deferred proposals, follow-up actions, dependencies on other working
groups, and breakout-session outcomes. This is materially richer than the TDoc index alone.

## Overlap And Timing

After whitespace normalization, using unique blocks of at least 20 characters:

- 3,050 of 3,088 chair-note blocks (98.8%) occur verbatim in the later final report.
- 118 of 121 post-meeting discussion blocks (97.5%) occur in the final report.
- Only 18 of 121 post-meeting discussion blocks (14.9%) occur in the chair notes.

The DOCX inside the final chair-notes ZIP is timestamped 2025-11-22. The official directory lists
the final report ZIP at 2026-02-17, nearly three months later. Chair notes therefore provide most
of the eventual report narrative early enough to support a useful provisional newsletter. The
final report should supersede them for the final edition.

## Pipeline Gaps Found During Audit

1. Working-group configurations do not discover `Inbox` or nested chair-note directories.
2. There is no chair-notes artifact kind or evidence authority.
3. Unrecognized metadata artifacts are stored raw and are not parsed into evidence.
4. Reports are parsed into blocks and evidence, but newsletter generation collects only evidence
   IDs attached to TDocs. Report evidence is not directly consumed by the newsletter packet.
5. Meeting-scoped passage retrieval expands meeting IDs to TDoc document IDs, which also excludes
   report and chair-note document IDs from that filtered retrieval path.

These five gaps are addressed by migration `20260902_0008` and the meeting-source enrichment
pipeline. The implementation adds nested source discovery, explicit source roles and authority,
immutable source versions, evidence-backed meeting observations, meeting-scoped source passage
retrieval, shared briefing API/MCP tools, and newsletter briefing consumption.

## Implemented Extraction Check

The final implementation was run against the downloaded RAN2#132 samples after preserving DOCX
paragraph/table order and coalescing evidence blocks:

| Artifact | Evidence blocks | Observations | TDoc-linked observations |
| --- | ---: | ---: | ---: |
| Final chair notes | 766 | 337 | 318 |
| Final report | 1,946 | 1,107 | 1,079 |
| Post-meeting email discussions | 6 | 90 | 41 |

The 337 chair-note observations comprise 92 decisions, 128 discussion summaries, 35 open issues,
13 follow-up actions, 36 dependencies, 17 deadlines, and 16 intended outcomes. Exact duplicate observations are collapsed by source
authority in a meeting briefing; full immutable source blocks remain available for audit and
focused retrieval.

## Other Omitted High-Value Artifacts

- Post-meeting email discussion records: intended outcomes, deadlines, rapporteurs, draft CRs,
  pending approvals, and work carried beyond the meeting.
- Consolidated and timestamped chair-note versions: useful for extracting what changed during the
  meeting, not just the end state.
- Breakout-session reports stored under chair-note directories: focused conclusions for technical
  sessions. Their TDoc bodies may be duplicated in `Docs`, but their role is currently lost.
- Non-CSV agenda and schedule documents: planned treatment order, breakout allocation, and
  schedule changes. Only `agenda.csv` is explicitly classified.
- Incoming and outgoing liaison bodies: duplicated in `Docs` for this sample, but their direction
  and liaison role are lost because `LSin` and `LSout` are not modelled as source roles.

The ordinary TDoc ZIPs found in `Inbox`, `Chair_Notes`, `LSin`, and `LSout` for RAN2#132 were all
also present in `Docs`. The missing value is primarily source role, narrative context, and lifecycle,
not duplicate TDoc content.

## Recommended Newsletter Use

1. Parse final or latest chair notes for provisional newsletters.
2. Resolve each chair-note observation to explicit TDoc IDs, agenda items, specifications, work
   items, and breakout sessions while retaining unlinked meeting-level observations.
3. Treat chair notes as provisional, source-qualified evidence below an approved or final report.
4. Ingest post-meeting email discussion records as pending-decision and follow-up observations.
5. Reconcile chair-note and email observations against the final report; publish changed,
   superseded, and unresolved items rather than silently overwriting them.
6. Use chair-note agenda structure and explicit joint treatment as constraints for semantic
   clustering. Use embeddings to discover additional relationships, not to replace chair intent.
