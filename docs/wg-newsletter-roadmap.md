# WG Analytical Newsletter Roadmap

Newsletter prose is intentionally outside the semantic-search branch. Implement it on
`wg-newsletter-generation` after the deterministic packet has been evaluated by senior telecom
engineers.

## Product unit

Generate one immutable report for each completed working-group meeting. The default comparison
window is the previous five meetings in the same WG and remains configurable. A provisional edition
uses spreadsheets and submitted TDocs; a final edition is created only after the authoritative
meeting report appears and includes a provisional-to-final change summary.

## Deterministic packet

The packet builder must scan every TDoc assigned to the meeting, without using a search-result cap.
It produces evidence-linked sections for material changes, decisions, rejected/postponed/unresolved
items, topic evolution, revision churn and merges, repeated unsuccessful proposals, specifications,
CRs, releases, work items, neutral company activity, contested topics, conclusion changes,
engineering implications, watch items, and a complete TDoc appendix.

Facts and implications remain separate. Deterministic ranking uses authority, final status,
revision depth, cross-company participation, specification impact, novelty, and persistence. Packet
records retain the scoring components so an engineer can audit why an item was included.

## LLM boundary

The configured OpenAI-compatible Qwen3-32B endpoint receives only the versioned packet and prompt.
It does not search the corpus or choose additional facts. Output is strict JSON. Every paragraph,
number, organization, specification, conclusion, and implication references packet evidence IDs.
Publication fails on unsupported content, invalid numbers, missing attribution, or unknown evidence.

Generation remains optional. The structured packet and packet-level MCP tool must work when the LLM
is unavailable or disabled. Initially, analytical prose requires human approval; approved and
rejected editions are retained for evaluation.

## Release gates

1. Domain reviewers score at least 20 completed meetings across RAN2, RAN3, SA2, and CT1 for
   omission, factual correctness, usefulness, neutrality, and citation quality.
2. Frozen endpoint tests cover malformed JSON, timeouts, unsupported claims, wrong numbers,
   attribution errors, and retry exhaustion.
3. A live Qwen3-32B run passes the same publication validator and human review threshold.
4. Scheduler tests prove delayed final reports create a new final edition without mutating the
   provisional edition.
5. `get_newsletter` remains backward compatible, while a packet-level MCP tool exposes deterministic
   input, scores, and evidence for audit.
