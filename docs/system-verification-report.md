# 3GPP Evidence Graph: System Verification Report

## Report identity

- Verification run: `20260829T073612Z`
- Verification date: 2026-08-29
- Proof root: [`artifacts/test-runs/20260829T073612Z/`](../artifacts/test-runs/20260829T073612Z/manifest.json)
- Dependency hashes and proof inventory: [`manifest.json`](../artifacts/test-runs/20260829T073612Z/manifest.json)
- Source revision: unavailable because this workspace is not a Git repository; lockfile hashes are recorded instead.

## Incremental verification: coarse evidence blocks and section navigation

- Verification run: `20260830T023807Z`
- Proof root: [`artifacts/test-runs/20260830T023807Z/`](../artifacts/test-runs/20260830T023807Z/pytest-junit.xml)
- Scope: evidence-block granularity, independent retrieval chunks, deterministic document-section
  trees, API/MCP section navigation, real RAN2 parser recovery, and browser document navigation.

| Check | Result | Proof |
|---|---:|---|
| Python suite | 145 passed, 1 optional PostgreSQL test skipped | [`pytest-junit.xml`](../artifacts/test-runs/20260830T023807Z/pytest-junit.xml) |
| Branch-aware coverage | 85.28% | [`coverage.json`](../artifacts/test-runs/20260830T023807Z/coverage.json) |
| Static analysis | Ruff and strict mypy passed for 35 source files | Local command transcript |
| Frontend lint | ESLint passed | Local command transcript |
| Incremental frontend production build | NOT COMPLETED: Vinext stalled after successful transforms while rendering client chunks and was terminated after two minutes | Known limitation; live dev UI was browser-verified |
| Worst-case real TDoc | 51,732 parser elements became 1,906 evidence blocks and 1,630 retrieval chunks; 96.32% block reduction; largest retrieval chunk 699 tokens | [`large-document-block-benchmark.json`](../artifacts/test-runs/20260830T023807Z/large-document-block-benchmark.json) |
| Live section API | Parent/child section nodes and bounded cursor returned from local PostgreSQL candidate | [`live-section-api.json`](../artifacts/test-runs/20260830T023807Z/live-section-api.json) |
| Browser document navigation | Collapsed hierarchy rendered; expansion and jump to `2 General / 2.4 Instructions` passed | [`ui-section-navigation.png`](../artifacts/test-runs/20260830T023807Z/ui-section-navigation.png), [`ui-section-tree-expanded.png`](../artifacts/test-runs/20260830T023807Z/ui-section-tree-expanded.png) |
| Fresh RAN2 canary | 1,142 metadata rows persisted; 9/10 original body attempts parsed and one exposed a bundled-workbook defect | [`ran2-coarse-canary.json`](../artifacts/test-runs/20260830T023807Z/ran2-coarse-canary.json) |
| Parser-fix RAN2 canary | Both selected TDocs and final report parsed after excluding separately ingested meeting-export workbooks from body archives | [`ran2-coarse-parser-fix-canary.json`](../artifacts/test-runs/20260830T023807Z/ran2-coarse-parser-fix-canary.json) |

Evidence blocks now target 1,000 tokens with a 1,400-token ceiling. Retrieval chunks remain
independently bounded to 300–700 tokens. Heading text is represented by the section tree instead of
being duplicated as an evidence row. The current `ran2-latest5-20260830` preview predates this format
and remains useful only for UI compatibility checks; production-quality comparisons require a new
immutable five-meeting candidate.

## Incremental verification: latest-five multi-WG corpus

- Verification date: 2026-08-30
- Candidate dataset: `latest5-all-wgs-20260830-v2`
- Scope: latest five substantive meetings for CT1, RAN2, RAN3, and SA2
- Reconciliation proof: [`latest5-reconciliation.json`](../artifacts/test-runs/20260830T023807Z/latest5-reconciliation.json)
- Full ingestion result: [`local-ingest-all-latest5.json`](../artifacts/test-runs/20260830T023807Z/local-ingest-all-latest5.json)
- Repair pass: [`local-ingest-repair-pass.json`](../artifacts/test-runs/20260830T023807Z/local-ingest-repair-pass.json)

| Check | Result | Classification |
|---|---:|---|
| Manifest artifacts represented in PostgreSQL by URL and SHA-256 | 22,578 / 22,578 (100%) | READY |
| Unique meeting/TDoc metadata | 20 meetings; 24,705 TDocs | READY WITH LIMITATIONS |
| Authoritative TDoc bodies parsed | 21,930 / 21,972 (99.81%) | READY WITH LIMITATIONS |
| Parsed or security-quarantined bodies | 21,968 / 21,972 (99.98%) | READY WITH LIMITATIONS |
| Evidence and retrieval storage | 298,349 blocks; 273,682 chunks | READY WITH LIMITATIONS |
| Graph integrity | 34,549 nodes; 204,374 edges; 0 missing endpoints | READY WITH LIMITATIONS |
| Post-ingestion deterministic suite | 157 passed, 1 optional PostgreSQL test skipped | PASS |

The deterministic parser recovered six real ISO Strict OOXML documents and one text withdrawal
notice during reconciliation. Two CT1-162 image-only PDFs remain failed because OCR is not
configured. Thirty-eight bodies remain intentionally quarantined by archive, macro, external-link,
package-validity, or workbook-size controls. Two legacy binary PowerPoint bodies are recorded as
unsupported, not parsed. CT1-162 and RAN3-133 have neither a published report nor an
evidence-backed normalized date in the mirrored source.

This candidate is **not activated**. Its activation gate correctly fails on the two OCR-dependent
documents and on report/date completeness for CT1-162 and RAN3-133. The corpus result proves source
accounting and deterministic ingestion behavior; it does not satisfy the expert retrieval,
relationship-accuracy, network load, security review, or live newsletter-model release gates.

## Incremental verification: complete single-meeting graph

- Verification date: 2026-08-30
- Candidate dataset: `latest5-all-wgs-20260830-v2`
- Graph rebuild proof: [`graph-rebuild.json`](../artifacts/test-runs/20260830T023807Z/graph-rebuild.json)
- Scope: canonical graph repair, complete single-meeting projection, meeting facets, and the
  single-meeting Sigma.js UI

| Check | Result | Classification |
|---|---:|---|
| Canonical graph rebuild | 20 meetings; 24,705 TDocs; 31,888 nodes; 172,688 edges | READY WITH LIMITATIONS |
| Containment reconciliation | Every meeting containment count equals its canonical TDoc count; 0 cross-meeting containment edges | READY |
| Largest exercised UI meeting (`SA2-173`) | 3,198 TDocs; 4,045 nodes; 18,951 edges; no truncation | [`ui-complete-sa2-173.jpg`](../artifacts/test-runs/20260830T023807Z/ui-complete-sa2-173.jpg) |
| Responsive reader at 900 px | Graph remained rendered; reader opened as an overlay with a close control | [`ui-complete-sa2-173-narrow.jpg`](../artifacts/test-runs/20260830T023807Z/ui-complete-sa2-173-narrow.jpg) |
| Live complete-graph API | HTTP 200 in 0.954 seconds; 843 KB compressed response | READY WITH LIMITATIONS |
| Meeting-specific autocomplete | `eri` ranked Ericsson first with 414 matching TDocs | READY |
| Filtered projection | Ericsson filter returned 414 TDocs, 664 nodes, and 3,837 edges | READY |
| Deterministic Python suite | 159 passed; 1 optional PostgreSQL test skipped | PASS |
| Isolated PostgreSQL integration | 1 passed against `threegpp_test` after all migrations | PASS |
| Frontend component suite | 3 passed | PASS |
| Static/build checks | Ruff, strict mypy for 39 files, ESLint, and production build passed | PASS |

The rebuild is atomic and idempotent for inactive datasets. Ingestion now refuses to create graph
facts for a TDoc canonically owned by another meeting. The complete endpoint retains typed node IDs,
edge evidence IDs, exact visible and total counts, and contextual boundary nodes for revision
predecessors. It rejects meetings above the configured 15,000-node or 75,000-edge ceiling instead
of returning a partial graph.

The browser exercised meeting selection, full SA2-173 rendering, autocomplete, immediate filtering,
and the wider cited TDoc reader. ForceAtlas2 runs in a worker and interactions remained available
during layout. Desktop and 900 px responsive rendering were visually checked; the latter retained
the graph and presented the reader as an overlay.

## Incremental verification: working-group revision graph

- Verification date: 2026-09-02
- Candidate dataset: `latest5-all-wgs-20260830-v2`
- Scope: complete WG projection, cross-meeting revision resolution, longest-chain calculation and
  Meeting/Working group UI scope selection

| Check | Result | Classification |
|---|---:|---|
| Complete RAN2 WG API | 5 meetings; 6,952 TDocs; 9,291 nodes; 47,816 edges | READY WITH LIMITATIONS |
| Revision continuity | 887 revision links; 138 cross-meeting links; longest chain 7 TDocs across RAN2-133-bis and RAN2-134 | READY |
| Graph integrity | 0 invalid endpoints; 7 longest-chain nodes and 6 connecting edges highlighted | READY |
| External boundary context | 174 predecessor TDocs outside the selected five-meeting corpus retained as boundary nodes | READY |
| Local compressed response | HTTP 200 in 2.17 seconds; 2.07 MB compressed | READY WITH LIMITATIONS |
| Python suite | 160 passed; 1 optional PostgreSQL test skipped in the general run | PASS |
| Isolated PostgreSQL integration | 1 passed | PASS |
| Frontend component suite | 5 passed, including Meeting to WG scope switching | PASS |
| Static/build checks | Ruff, strict mypy for 39 files, ESLint and production build passed | PASS |

The WG projection uses the same canonical TDocs and evidence links as meeting scope. Revision
predecessors owned by another selected meeting are normal nodes; only predecessors outside the
available WG corpus are boundaries. The longest chain is calculated deterministically from
`revised_from`, and its TDocs and revision edges are marked for stronger Sigma.js rendering.

The local API and frontend component/build paths were verified, but a new interactive browser
screenshot was not captured because the browser-control backend was unavailable during this run.
The 2.17-second first request is slightly above the two-second production target and needs a
networked repeated-load measurement before production classification can be raised.

## Incremental verification: local ONNX semantic search

- Verification date: 2026-09-01
- Proof root: `artifacts/test-runs/semantic-pre-migration-20260901T161014Z/` (local, ignored by Git)
- Model: `ibm-granite/granite-embedding-english-r2`
- Immutable revision: `47ea694b257b703fee9253d75c2b1f2985180498`
- Profile: `emb-0bdf6b54f20827a5de1b924c14a03f0b` (768 dimensions, normalized, O3 ONNX)
- Dataset: `ran2-20260829-canary-134` (789 chunks in four parsed documents)
- Reference host: Apple M4, local PostgreSQL 17 with pgvector

| Check | Result | Classification |
|---|---:|---|
| Python suite | 181 passed, including two PostgreSQL integration tests | PASS |
| Ruff / strict mypy / migration drift | Ruff passed; mypy passed for 42 source files; Alembic reported no new operations | PASS |
| Repository-wide coverage | 79.11% combined line/branch score; below configured 85% gate | NOT READY |
| Resumable real backfill | 789/789 vectors; 100% coverage; initial immutable-profile build took 552 s; second run embedded 0 duplicate chunks and completed in 0.081 s | READY |
| Profile-specific HNSW | Valid partial expression index built for the 768-dimensional profile | READY |
| ONNX/reference parity | Minimum cosine 0.99999988; maximum absolute difference 0.0000444 across three technical queries | PASS |
| Warm single-query latency | p50 63.7 ms; p95 83.9 ms | PASS (<250 ms) |
| Document throughput | 2.08 retrieval chunks/s for a 64-chunk mixed-length sample | READY WITH LIMITATIONS |
| Runtime memory | 3.88 GB maximum RSS; 3.11 GB peak footprint reported during benchmark process | READY WITH LIMITATIONS |
| Eight-request concurrency gate | 16 requests; p50 896 ms; p95 930 ms; 8.83 requests/s; 0 errors | PASS (<2 s) |
| Hybrid/evidence behavior | 16/16 responses reported `hybrid`, 16/16 returned evidence, and health matched the active profile | PASS |
| PostgreSQL integration | Variable 2D/3D profiles, two HNSW indexes, profile isolation, ranking, atomic switch, and advisory-lock/index regression passed | PASS |
| Migration exercises | Existing legacy vectors preserved; fresh install, downgrade/upgrade, and Alembic drift checks passed | PASS |
| Offline provider tests | Revision pinning, acquisition/export mocks, checksums, path safety, pooling, batching, normalization, malformed vectors, interruption, idempotency, failure state, and lexical fallback passed | PASS |
| 150-question expert Evidence Recall@10 | NOT RUN: no expert-authored and adjudicated question set exists; this canary has only four parsed documents | NOT READY |
| Challenger comparison | 311M and 97M Granite profiles were not backfilled or quality-scored | NOT READY |
| Production 50 RPS gate | Not run; production server hardware is undefined | NOT READY |

The first real index build exposed a self-deadlock: the advisory-lock query had opened a transaction,
and `CREATE INDEX CONCURRENTLY` waited for that same transaction. The blocked index statement was
cancelled without losing vectors. The lock now uses autocommit, invalid indexes are removed before a
retry, a regression integration test enforces a five-second completion bound, and the resumed run
reused all 789 vectors. This failure-and-recovery evidence supports resumability; it does not replace
a longer operational soak. A prior valid profile remains stored as `superseded`, while the corrected
profile identity includes ONNX optimization, selected file, and execution provider.

The runtime and PostgreSQL mechanisms are **READY WITH LIMITATIONS**. Latency and parity gates pass,
but retrieval quality is not production-approved. The canary cannot support an honest expert
Evidence Recall@10 claim because only four source documents have chunks. A synthetic query set would
not be equivalent to expert adjudication, so the report leaves this mandatory gate open.

## Readiness conclusion

**Overall status: NOT READY for production.**

The implementation is a working, tested vertical slice: configuration, source discovery, artifact
handling, evidence-addressable parsing, temporal graph structures, hybrid retrieval, MCP contracts,
job leases, atomic publication, deterministic briefing packets, and the graph UI are present. The
local deterministic suite passes, the selected high-risk modules exceed the requested coverage and
mutation thresholds, and live source-directory validation passed for four meetings in each target
working group.

Production release is blocked because mandatory acceptance evidence is still absent: no complete
24-month backfill, no 150-question expert retrieval evaluation, no
production-network load test, and no live newsletter-generation gate. The UI dependency audit also
reports 12 high-severity advisories. Local PostgreSQL, pgvector, atomic activation, and database
backup/restore are now verified; S3 is not part of the selected single-host architecture. This report
therefore does not claim that the system works perfectly.

## Verified results

| Check | Result | Proof |
|---|---:|---|
| Python tests | 120 passed, 0 failed, including local PostgreSQL | [`pytest-junit.xml`](../artifacts/test-runs/20260829T073612Z/pytest-junit.xml) |
| Overall line/branch-aware coverage score | 89.20% | [`coverage.json`](../artifacts/test-runs/20260829T073612Z/coverage.json) |
| Overall branch coverage | 77.73% | [`coverage.json`](../artifacts/test-runs/20260829T073612Z/coverage.json) |
| Core branch coverage | normalization, retrieval, topics, newsletter, publisher: 100%; graph: 94.44% | [`coverage.json`](../artifacts/test-runs/20260829T073612Z/coverage.json) |
| Mutation score on selected core modules | 480/563 killed, 85.26% | [`mutation-summary.json`](../artifacts/test-runs/20260829T073612Z/mutation-summary.json) |
| Static analysis | Ruff passed; strict mypy passed for 33 source files | Local verification command transcript |
| UI checks | ESLint passed; production build passed | Local verification command transcript |
| Live 3GPP source validation | RAN2, RAN3, SA2 passed, four meetings each | [`source-validation.json`](../artifacts/test-runs/20260829T073612Z/source-validation.json) |
| Fixture MCP transcripts | Three representative requests with evidence envelopes | [`mcp-transcripts.json`](../artifacts/test-runs/20260829T073612Z/mcp-transcripts.json) |
| In-process concurrency probe | 300 users, 600 requests, 0 errors; graph p95 2.241 ms | [`local-load-probe.json`](../artifacts/test-runs/20260829T073612Z/local-load-probe.json) |
| Local PostgreSQL/pgvector | PostgreSQL 17.11, pgvector 0.8.6; FTS, vector ordering, indexes and atomic activation passed | JUnit |
| PostgreSQL backup/restore | Fixture dump restored with both required indexes and all retrieval chunks | [`postgres-backup-restore.json`](../artifacts/test-runs/20260829T073612Z/postgres-backup-restore.json) |
| UI dependency audit | 13 advisories: 12 high, 1 low | [`npm-audit.json`](../artifacts/test-runs/20260829T073612Z/npm-audit.json) |

The overall branch percentage is below the general 85% target because optional and operational
branches remain unexercised. PostgreSQL-specific full-text, vector, dataset binding, index and atomic
publication paths now execute in the integration suite. The explicitly named core normalization,
evidence, retrieval, and publication modules meet or exceed 85% branch coverage.

## Subsystem classification

| Subsystem | Classification | Basis and remaining work |
|---|---|---|
| Configuration and WG adapters | READY WITH LIMITATIONS | Typed, centralized configuration and data-driven RAN2/RAN3/SA2 adapters pass unit and live listing tests. Full backfill is unverified. |
| Download, parse, normalize, chunk | READY WITH LIMITATIONS | Idempotency, conditional fetch, security limits, XLSX/DOCX/PDF/ZIP and stable evidence chunks pass fixtures. Legacy `.doc` conversion is fail-closed but not implemented. |
| Graph and evidence model | READY WITH LIMITATIONS | Direction, cycle, orphan, temporal and evidence contracts pass. Cross-WG accuracy has not been measured on a full corpus. |
| Hybrid retrieval | READY WITH LIMITATIONS | Pinned local ONNX, profile isolation, resumable vectors, HNSW, fallback, parity and M4 latency pass. Expert Recall@10 and challenger comparison remain unverified. |
| MCP/API/OIDC | READY WITH LIMITATIONS | Required tools, envelopes, temporal argument rules, pagination, bounds and OIDC validation pass tests. No deployed identity-provider integration or network soak test. |
| Scheduler and publication | READY WITH LIMITATIONS | Idempotent jobs, leases, reclaim, retry and dead-letter pass component tests; atomic activation also passes PostgreSQL. Continuous operation is unverified. |
| Deterministic newsletter packet | READY WITH LIMITATIONS | Evidence-backed packet generation and publication guards pass. Corpus-level usefulness has not been evaluated by domain experts. |
| Newsletter prose generation | NOT READY | Frozen endpoint behavior passes; no live generative-model gate was run. The feature flag remains disabled. |
| Graph UI/document reader | READY WITH LIMITATIONS | Complete single-meeting projections, meeting-scoped autocomplete, worker layout, section navigation, desktop reader resizing and the narrow-screen overlay are browser/component-verified. Dependency advisories and formal accessibility evidence remain open. |
| Backup, restore and disaster recovery | READY WITH LIMITATIONS | Local `pg_dump`/`pg_restore` passed. Full-corpus filesystem snapshot, point-in-time recovery and disaster drill remain unverified. |

## Requirement-to-test traceability

| Requirement | Module | Test identifiers | Result | Proof |
|---|---|---|---|---|
| Central typed configuration; no embedded endpoint/model secrets | `config.py`, YAML, environment loader | `test_default_configuration_loads`, `test_environment_override`, `test_production_configuration_requires_*` | PASS locally | JUnit, redacted config |
| WG extensibility and source conventions | `sources/*`, WG YAML, source model | `test_working_groups_are_data_driven`, `test_ran2_*`, `test_sa2_*`, live validation | PASS for sampled meetings | source validation |
| Conditional, immutable, idempotent downloads | downloader and local object store | `test_download_hashes_*`, `test_download_retries_*`, `test_local_object_store_*` | PASS in fixtures | JUnit |
| Safe parsing and evidence-addressable blocks | parsers, chunker, evidence domain | parser, archive, macro, relationship, oversized-block and stable-ID tests | PASS in fixtures | JUnit, coverage |
| Normalization of statuses and high-value metadata | normalize module | all `test_normalization.py` parameter cases | PASS; 100% branch coverage | JUnit, coverage |
| Revision graph integrity and temporal relationships | graph validation | orphan, direction, cycle and valid-chain tests | PASS; 94.44% branch coverage | JUnit, mutation |
| Dataset validation and atomic activation | pipeline, publisher | idempotent ingestion, publisher guards and PostgreSQL integration test | PASS on SQLite and PostgreSQL | JUnit |
| Exact, lexical, vector and hybrid retrieval | retrieval, repository | scoring, RRF, pgvector, PostgreSQL FTS, filters, temporal scope and service tests | PASS locally | JUnit, mutation |
| Versioned local ONNX profiles and resumable backfill | ONNX provider, embedding backfill, profile schema | `test_onnx_*`, `test_backfill_*`, PostgreSQL multi-profile and advisory-lock tests | PASS with quality limitation | local ONNX proof root, JUnit |
| Every factual tool result includes evidence and dataset version | MCP contracts and service | `test_every_mcp_tool_executes_with_evidence_envelope`, passage evidence test | PASS in fixtures | JUnit, MCP transcripts |
| Required MCP tool inventory and argument rules | MCP server, domain schemas | tool registration, temporal exclusivity and cursor tests | PASS | JUnit |
| OIDC/OAuth and bounded API behavior | security middleware, API | OIDC discovery/JWKS, protected routes, graph bounds and missing document tests | PASS with mocks | JUnit |
| Self-healing jobs, leases and dead-letter handling | scheduler, worker | lease, duplicate, expiry, owner, backoff, dead-letter and handler tests | PASS on SQLite | JUnit |
| Deterministic newsletter and unsupported-claim rejection | newsletter packet and renderer | deterministic packet, absent/incomplete, citations, numbers and organizations tests | PASS with frozen model responses | JUnit, coverage |
| OpenAI-compatible model failure behavior | model client | auth, timeout/rate-limit retry, malformed/partial response, dimension and provider tests | PASS with `respx` | JUnit |
| Searchable graph UI and document reader | `web/`, graph API | API route tests, ESLint, production build, live section expansion and jump | PASS locally with limitations | npm audit, build output, incremental UI screenshots |
| Local database backup and restore | PostgreSQL/pgvector | `pg_dump`, `pg_restore`, row/index/version reconciliation | PASS for fixture corpus | backup/restore proof |
| 24-month production corpus and networked load targets | deployment/infrastructure | none completed | NOT RUN | environment manifest |

## Acceptance criteria

| Criterion | Result | Explanation |
|---|---|---|
| At least 99.5% of available TDoc rows ingested | PASS FOR LATEST-FIVE CORPUS | 100% of 22,578 selected artifacts are accounted for; 99.81% of authoritative bodies parsed and 99.98% parsed or safely quarantined. The 24-month production scope remains untested. |
| At least 99% fidelity for high-value spreadsheet fields | NOT RUN | Fixture normalization passes, but no corpus-scale gold comparison exists. |
| At least 98% correct revision, merge, liaison, report and CR relationships | NOT RUN | Structural tests pass; no expert-labeled corpus was scored. |
| 150 expert questions and Evidence Recall@10 at least 90% | NOT RUN | Evaluation set has not been created or adjudicated. |
| Every factual MCP result contains valid evidence | PASS IN FIXTURES | Every required tool executes with an evidence envelope against the deterministic dataset. |
| Reprocessing creates no duplicate records | PASS IN FIXTURES | End-to-end idempotent ingestion passes; full-corpus PostgreSQL reprocessing remains unverified. |
| Interrupted ingestion recovers without manual repair | PASS IN COMPONENT TESTS | Expired leases, retry and dead-letter behavior pass; no production crash drill. |
| Concurrent reads observe one complete dataset version | PASS IN INTEGRATION TEST | PostgreSQL row locking and atomic active-version replacement pass; concurrent reader soak remains pending. |
| 300 users/50 reads per second, metadata p95 under 500 ms and retrieval p95 under 2 s | PARTIAL | Local ONNX hybrid retrieval passed at eight concurrent requests with p95 835 ms, but the production 50 RPS/network/hardware gate remains untested. |
| Clean install, backfill, update, rollback, backup and restore | PARTIAL | Local PostgreSQL installation, migration and fixture backup/restore pass; corpus backfill, rollback and filesystem restore remain. |

## Security and operational findings

The parser rejects macros, local/external Office relationships, path traversal, oversized downloads,
and unsafe archives. The downloader uses a source-host allowlist and OIDC routes validate discovery,
JWKS, issuer, audience and authentication behavior in mocked tests.

These checks are necessary but not sufficient. Before release, update or replace the affected UI
dependencies and rerun `npm audit`; run DAST and SSRF tests against the deployed network policy;
exercise OIDC with the production identity provider; and verify local filesystem permissions,
encryption, retention and snapshot restoration.

## Required release work

1. Execute the 24-month RAN2, RAN3 and SA2 backfill into the installed local PostgreSQL/pgvector
   database and content-addressed filesystem store, with row-level reconciliation.
2. Implement or provision sandboxed legacy `.doc` conversion and reconcile malformed/legacy files.
3. Build and expert-label at least 150 representative architect/engineer questions; measure and tune
   Evidence Recall@10 and relationship accuracy.
4. Resolve the high-severity UI dependency advisories and capture formal accessibility evidence.
5. Run networked load, failover, rollback, point-in-time database recovery and full filesystem
   snapshot restoration exercises.
6. Expert-score the pinned 149M embedding profile and the 311M/97M challengers. Retain the default
   only if Evidence Recall@10 reaches 90% and the challenger policy is satisfied.
7. Run the separate live generative-model evaluation before enabling newsletter prose. Newsletter
   generation must remain disabled until that gate passes.

## Reproduction

```bash
uv sync --all-groups --extra onnx --no-editable
uv run ruff check .
uv run mypy src/threegpp_kg
uv run pytest --cov=threegpp_kg --cov-branch
uv run mutmut run
cd web
npm install
npm run lint
npm run build
```

The local concurrency probe and transcript generators are `scripts/local_load_probe.py` and
`scripts/mcp_transcript.py`. The proof manifest is generated with
`scripts/create_proof_manifest.py`.
