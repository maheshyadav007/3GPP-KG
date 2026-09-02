# 3GPP Evidence Graph

Evidence-first ingestion, retrieval, MCP tools, and graph exploration for 3GPP meetings.

The backend is managed with `uv`; the UI lives in `web/`. Configuration is loaded from
`config/defaults.yaml`, WG-specific YAML files, and `THREEGPP_*` environment variables.

## Development

```bash
uv sync --all-groups --extra onnx --no-editable
uv run pytest
uv run threegpp-kg serve
```

The API and MCP endpoint default to `http://localhost:8000`; Streamable HTTP MCP is mounted at
`/mcp`. Start the graph UI separately with `cd web && npm install && npm run dev` and open
`http://localhost:3000`.

Run the complete local quality suite with:

```bash
uv run ruff check .
uv run mypy src/threegpp_kg
uv run pytest --cov=threegpp_kg --cov-branch
uv run mutmut run
cd web && npm run lint && npm run build
```

The default development database is SQLite so the application and test suite can run without
external infrastructure. The intended single-host deployment uses local PostgreSQL with pgvector
and the content-addressed filesystem store under `data/artifacts`; S3 is optional, not required.

## Clone And Build A Local Graph

The Git repository contains source code, schemas, configuration, tests, and documentation only.
Downloaded 3GPP artifacts, PostgreSQL data, generated manifests, and test-run evidence are ignored.
After cloning, create the Python and frontend environments with:

```bash
uv sync --all-groups --extra onnx --no-editable
cd web && npm ci && cd ..
```

Start PostgreSQL with pgvector using Docker, initialize the schema, and ingest the latest five
meetings for each configured working group:

```bash
docker compose up -d postgres
export THREEGPP_DATABASE_MODE=sql
export THREEGPP_DATABASE_URL=postgresql+asyncpg://threegpp:local-development-only@localhost:5432/threegpp
uv run alembic upgrade head

for wg in RAN2 RAN3 SA2 CT1; do
  uv run threegpp-kg backfill \
    --working-group "$wg" \
    --last-k 5 \
    --dataset-version local-latest-five \
    --document-limit -1
done
```

Chair notes, meeting reports, and post-meeting email discussions are discovered and ingested by
the normal backfill. To enrich meetings already present in a non-active candidate without
reprocessing TDoc bodies, run:

```bash
uv run threegpp-kg enrich-meeting-sources \
  --working-group RAN2 \
  --meeting 132 \
  --dataset-version local-latest-five \
  --output artifacts/ran2-132-source-enrichment.json
```

The enrichment is content-hash idempotent. An unchanged legacy report is reparsed once from the
local object store to create source metadata and observations; later runs skip it. Active datasets
remain immutable, so enrich the candidate before activation.

Activation validates completeness before making the candidate dataset current. If a newly
finished meeting has not published its report yet, run the API against the immutable candidate as
a preview until the missing source appears:

```bash
uv run threegpp-kg activate-dataset --dataset-version local-latest-five

# Preview an inactive candidate when activation correctly rejects incomplete source material.
export THREEGPP_DATABASE_PREVIEW_DATASET_VERSION=local-latest-five
uv run threegpp-kg serve
```

In another terminal, run `cd web && npm run dev`, then open `http://localhost:3000`. The MCP
Streamable HTTP endpoint is `http://localhost:8000/mcp`.

The graph UI supports two complete scopes: one meeting or every stored meeting in one working
group. Working-group scope resolves cross-meeting TDoc revisions as ordinary graph links, reports
the longest revision chain, and highlights that chain in the graph. Choose the scope first, then
narrow its TDocs with company, topic, and specification autocomplete filters. Graph responses are
never silently sampled: a scope above its configured safety ceiling returns an explicit error. The
document reader is resizable on desktop and opens as an overlay on narrower screens.

On macOS, install and initialize the local database with:

```bash
brew install postgresql@17 pgvector
brew services start postgresql@17
/opt/homebrew/opt/postgresql@17/bin/createdb threegpp
/opt/homebrew/opt/postgresql@17/bin/psql -d threegpp -c \
  'CREATE EXTENSION IF NOT EXISTS vector;'
THREEGPP_DATABASE_MODE=sql \
THREEGPP_DATABASE_URL=postgresql+asyncpg://localhost:5432/threegpp \
  uv run alembic upgrade head
```

Use the same two environment variables when starting `threegpp-kg serve`. The default
`database.mode=fixture` intentionally keeps the demo UI usable before a corpus has been ingested.

PostgreSQL full-text and structured retrieval work without a model. pgvector semantic search needs
embeddings from either a small local embedding model or a configured embedding endpoint; embedding
and reranking are optional. Only final newsletter prose requires a generative LLM.

## Local ONNX Semantic Search

The portable default is the pinned `ibm-granite/granite-embedding-english-r2` ONNX profile: 768
dimensions, normalized vectors, CPU execution, 2,048-token documents, and 256-token queries. Model
artifacts and vectors are never committed. Only an explicit backfill command may download and export
the model; API processes load the cached ONNX package with local-files-only mode.

```bash
uv sync --all-groups --extra onnx
export THREEGPP_DATABASE_MODE=sql
export THREEGPP_DATABASE_URL=postgresql+asyncpg://localhost:5432/threegpp
uv run alembic upgrade head

uv run threegpp-kg backfill-embeddings \
  --dataset-version local-latest-five \
  --profile default \
  --activate \
  --output artifacts/embedding-backfill.json

uv run threegpp-kg benchmark-embeddings \
  --dataset-version local-latest-five \
  --sample-size 64 \
  --output artifacts/embedding-benchmark.json
```

Embedding profiles are immutable and isolated by dataset. Backfills skip existing vectors, commit
length-bucketed batches, recover after interruption, validate coverage and vector invariants, build
a profile-specific HNSW expression index, and atomically switch the active profile. Re-run an older
cached configuration with `--activate` to roll back. A missing/corrupt model, profile mismatch, or
inference failure returns lexical results with an explicit `lexical_fallback` warning.

The active profile, revision, dimensions, coverage, cache availability, and readiness are exposed by
`GET /health`. Passage responses identify the profile and whether retrieval was `hybrid`, `lexical`,
or `lexical_fallback`. All model, revision, cache, length, batching, pooling, prompt, thread,
concurrency, and download settings live in `config/defaults.yaml` and documented `THREEGPP_*`
environment variables.

Document parsers recover detailed Office structure in memory, then coalesce adjacent source
elements into section-aware evidence blocks before persistence. Evidence blocks target 1,000 tokens
for compact, human-readable citations; independent 300-700-token retrieval chunks preserve search
quality. Headings become a deterministic document-section tree rather than duplicate body rows.
See `docs/evidence-policy.md`.

## WG Analytical Newsletters

Newsletter processing has two independent stages. The deterministic stage scans every TDoc in a
meeting, compares the previous five meetings in the same WG, scores material signals, and persists
an immutable evidence packet. It does not require an LLM. Optional prose rendering sends only the
bounded, versioned packet to the configured OpenAI-compatible Qwen endpoint and validates every
paragraph against packet evidence before retaining it for human approval.

```bash
export THREEGPP_DATABASE_MODE=sql
export THREEGPP_DATABASE_URL=postgresql+asyncpg://localhost:5432/threegpp
uv run alembic upgrade head

# Build and persist a provisional evidence packet without an LLM.
uv run threegpp-kg build-newsletter \
  --meeting RAN2-135 \
  --edition provisional \
  --last-k-meetings 5 \
  --output artifacts/ran2-135-newsletter-packet.json

# Render only after configuring and enabling the generation endpoint.
THREEGPP_NEWSLETTER_GENERATION_ENABLED=true \
THREEGPP_GENERATION_BASE_URL=https://internal.example/v1 \
THREEGPP_GENERATION_API_KEY=redacted \
  uv run threegpp-kg build-newsletter \
    --meeting RAN2-135 --edition provisional --render

uv run threegpp-kg review-newsletter \
  --newsletter-id <newsletter-id> \
  --decision approve \
  --reviewer <reviewer-id>
```

The API exposes packet generation, immutable records, and review at
`/api/newsletters/{meeting_id}`, `/api/newsletters/{meeting_id}/generate`,
`/api/newsletters/{meeting_id}/record`, and `/api/newsletter-reviews/{newsletter_id}`. MCP clients
can use `get_newsletter`, `get_newsletter_packet`, and `get_published_newsletter`. A final edition is
separate from the provisional edition and includes an evidence-backed provisional-to-final delta.
See `docs/wg-newsletter-roadmap.md` for the packet contract and current release gates.

## Latest-Five Local Corpus

The current local candidate contains five substantive meetings each for CT1, RAN2, RAN3, and SA2.
Source acquisition and parsing are separate: manifests make downloads resumable, while local
ingestion performs no network access. Reconcile every manifest URL and hash after ingestion with:

```bash
THREEGPP_DATABASE_MODE=sql \
THREEGPP_DATABASE_URL=postgresql+asyncpg://localhost:5432/threegpp \
PYTHONPATH=src \
  uv run python scripts/reconcile_manifests.py \
    --dataset-version latest5-all-wgs-20260830-v2 \
    --manifest data/download-manifests/ct1-latest5.json \
    --manifest data/download-manifests/ran2-latest5.json \
    --manifest data/download-manifests/ran3-latest5.json \
    --manifest data/download-manifests/sa2-latest5.json \
    --output artifacts/latest5-reconciliation.json
```

Activation is a separate validation gate. It fails when source coverage, reports, parser status, or
body evidence is incomplete:

```bash
THREEGPP_DATABASE_MODE=sql \
THREEGPP_DATABASE_URL=postgresql+asyncpg://localhost:5432/threegpp \
  uv run threegpp-kg activate-dataset \
    --dataset-version latest5-all-wgs-20260830-v2
```

If canonical TDoc ownership changes or a cumulative meeting spreadsheet previously contaminated
meeting containment, rebuild an **inactive** dataset graph from canonical `tdocs.meeting_id` values:

```bash
THREEGPP_DATABASE_MODE=sql \
THREEGPP_DATABASE_URL=postgresql+asyncpg://localhost:5432/threegpp \
  uv run threegpp-kg rebuild-graph \
    --dataset-version latest5-all-wgs-20260830-v2 \
    --output artifacts/graph-rebuild.json
```

The command is idempotent and validates one `contains` edge per TDoc, endpoint integrity,
deduplication, and absence of cross-meeting containment before committing the replacement graph.

The current candidate is intentionally not active: two image-only CT1 PDFs require OCR, and the
source has no published report for CT1-162 or RAN3-133. See the reconciliation artifact and
`docs/system-verification-report.md` for exact counts.

## Readiness

See `docs/system-verification-report.md`. Deterministic newsletter packets are available with
documented limitations. Newsletter prose generation remains disabled and not production-ready until
a configured live Qwen model and human domain-review gate pass.
