# Meeting Source Enrichment

## Data Model

Each report, chair note, or post-meeting discussion is stored as a complete immutable artifact
version. `logical_document_id` groups versions of the same source, while `document_id` identifies
the parsed blocks for one content hash. The pipeline never stores a diff instead of the source.

`meeting_observations` contains compact, evidence-linked facts extracted from those versions:

- decisions and conclusions;
- discussion summaries;
- open issues and dependencies;
- follow-up actions and deadlines;
- intended outcomes.

Each observation retains its source role, authority, agenda item, TDoc/specification links,
content hash, and evidence IDs. Briefing diffs compare the latest two versions of each logical
source and classify observations as added, removed, or changed.

## Existing Datasets

Existing TDoc rows and bodies do not need to be downloaded or parsed again. Apply the migration,
then enrich meetings already present in a building or validated candidate:

```bash
uv run alembic upgrade head
uv run threegpp-kg enrich-meeting-sources \
  --working-group RAN2 \
  --meeting 132 \
  --dataset-version candidate-v1
```

The command processes only `reports`, `chair_notes`, and `post_meeting_discussion` roles. Legacy
reports already in the object store are reparsed locally once when their source-version metadata
is absent. HTTP validators and content hashes make subsequent runs no-ops. Active datasets are
immutable and must not be enriched in place.

## Shared Interfaces

REST:

- `GET /api/meetings/{meeting_id}/sources`
- `GET /api/meetings/{meeting_id}/source-content?document_id=...`
- `GET /api/meetings/{meeting_id}/briefing?edition=provisional|final`

MCP:

- `list_meeting_sources`
- `get_meeting_source`
- `get_meeting_briefing` and backward-compatible `get_meeting_brief`
- `get_meeting_changes`
- `get_meeting_timeline`
- `get_meeting_decisions`

`newsletter_packet` calls `meeting_briefing` and converts its highest-value observations into the
same bounded, evidence-backed signal list used by newsletter rendering. No separate chair-note
prompt or full-document LLM pass exists.
