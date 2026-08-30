# Evidence Policy

Every factual API or MCP result carries an evidence reference and dataset version. Evidence points
to an immutable artifact hash and, when available, a section path and block identifier. Short
excerpts help the caller inspect relevance but never replace the original source link.

The service distinguishes exact metadata, report-confirmed facts, document-supported facts,
model-extracted facts, and inferences. Unsupported claims are omitted. Company activity means a
company is listed as a source or explicitly named in evidence; it does not imply an unstated company
position. `Revised`, `not pursued`, `postponed`, and `rejected` remain distinct conclusions.

Dataset publication is atomic. A request is evaluated entirely against the dataset version active
when that request begins.

## Evidence block granularity

Office parsers first recover source elements such as headings, paragraphs, list items, and table
rows. Those source elements are not persisted one-for-one. Before storage, adjacent elements in the
same section are deterministically combined into evidence blocks using the independent
`evidence_blocks` token limits. The default target is 1,000 tokens with a 1,400-token ceiling.

Heading text is stored once in the section path and deterministic document-section tree, rather
than duplicated as a body block. Discussions, agreements, conclusions, notes, section changes, and
table/body boundaries remain explicit boundaries. Ordinary paragraphs and list items may share a
block, and adjacent table rows may share a table block. Each persisted block retains the document,
section path, kind, stable identifier, and source artifact hash through its evidence record.

Retrieval chunks use separate 300-700-token limits. A large evidence block may therefore produce
multiple search chunks, all pointing back to the same evidence block and immutable artifact. This
provides compact storage and section-level citation precision without creating a database row for
every Word paragraph or table row. The original immutable artifact remains the final authority
when finer inspection is required.

## Document section tree

Every parsed document exposes a model-free section tree derived from DOCX heading paths, PDF pages,
or workbook sheets. Each node records its parent, child count, direct and descendant block counts,
and stable start/end block indexes. SQL, full-text, or semantic retrieval first selects candidate
TDocs; the tree then supports bounded navigation inside long documents. LLM-generated node
summaries are deliberately excluded from the ingestion dependency chain.
