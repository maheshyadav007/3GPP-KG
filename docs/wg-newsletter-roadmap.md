# WG Analytical Newsletter Design and Release Status

The deterministic newsletter pipeline is implemented on `wg-newsletter-generation`. It produces
auditable meeting packets without an LLM and can optionally render them through the configured
OpenAI-compatible Qwen3-32B endpoint. Prose rendering remains disabled by default and is not
production-ready because no live endpoint or telecom-review gate has been completed.

## Product unit

Generate one immutable report for each completed working-group meeting. The default comparison
window is the previous five meetings in the same WG and remains configurable. A provisional edition
uses spreadsheets and submitted TDocs; a final edition is created only after the authoritative
meeting report appears and includes a provisional-to-final change summary.

## Deterministic packet

The packet builder scans every TDoc assigned to the meeting without using a search-result cap. It
produces evidence-linked sections for material changes, decisions, rejected/postponed/unresolved
items, topic evolution, revision churn and merges, repeated unsuccessful proposals, specifications,
CRs, releases, work items, neutral company activity, contested topics, conclusion changes,
engineering implications, watch items, and a complete TDoc appendix.

Facts and implications remain separate. Deterministic ranking uses authority, final status,
revision depth, cross-company participation, specification impact, novelty, and persistence. Packet
records retain the scoring components so an engineer can audit why an item was included.

Packet identity is derived from canonical content and the immutable dataset version. Rebuilding the
same edition is idempotent; a changed source dataset creates a new edition instead of overwriting an
old one. Evidence entries retain meeting, TDoc, source URL, artifact hash, section, block, excerpt,
authority, confidence, and extraction metadata.

## LLM boundary

The configured OpenAI-compatible Qwen3-32B endpoint receives only a bounded rendering view of the
versioned packet and prompt. It does not receive the complete TDoc appendix, search the corpus, or
choose additional facts. Output is strict JSON. Every paragraph, number, organization,
specification, conclusion, and implication references packet evidence IDs. Publication fails on
unsupported content, invalid numbers, missing attribution, or unknown evidence.

Generation remains optional. The structured packet and packet-level MCP tool must work when the LLM
is unavailable or disabled. Initially, analytical prose requires human approval; approved and
rejected editions are retained for evaluation.

## Operational interfaces

- CLI: `build-newsletter` creates an immutable packet and optionally renders it; `review-newsletter`
  approves or rejects rendered content.
- API: packet, record, generate, and review endpoints expose the same workflow.
- MCP: `get_newsletter` remains packet-compatible; `get_newsletter_packet` is the explicit audit
  interface; `get_published_newsletter` returns persisted editions.
- Scheduler: meeting ingestion enqueues provisional generation, while a delayed authoritative
  report enqueues a distinct final edition. Immutable idempotency keys prevent duplicate jobs.

## Current verification

- The complete Python/PostgreSQL suite passes with 190 tests.
- A real RAN2-135 provisional packet scanned all 1,341 assigned TDocs and produced a complete 1,341
  item appendix without a model.
- Repeated builds under different `PYTHONHASHSEED` values produced one record with the same packet
  ID and SHA-256.
- Frozen endpoint tests cover strict JSON, missing sections, unknown citations, wrong numbers,
  unsupported organizations/specifications/conclusions, generation failure retention, and review.
- Final-edition scheduling and provisional-to-final delta behavior pass deterministic tests.
- Repository-wide coverage is 80.34%, below the configured 85% gate; the newsletter module is 92%.
- The full mutation score is 69.32%, below the required 80% gate.
- No live Qwen3-32B run or 20-meeting senior telecom review has been completed.

## Release gates

1. Domain reviewers score at least 20 completed meetings across RAN2, RAN3, SA2, and CT1 for
   omission, factual correctness, usefulness, neutrality, and citation quality.
2. Frozen endpoint tests cover malformed JSON, timeouts, unsupported claims, wrong numbers,
   attribution errors, and retry exhaustion.
3. A live Qwen3-32B run passes the same publication validator and human review threshold.
4. Scheduler tests prove delayed final reports create a new final edition without mutating the
   provisional edition.
5. Raise repository coverage to 85% and mutation score to 80%, then rerun the full proof manifest.
6. Keep `get_newsletter` backward compatible while the packet-level MCP tool exposes deterministic
   input, scores, and evidence for audit.

Current classification: deterministic packets are **READY WITH LIMITATIONS**; Qwen-rendered prose is
**NOT READY** and the generation feature flag must remain disabled.
