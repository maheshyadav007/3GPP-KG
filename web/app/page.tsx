'use client';

import { Building2, CalendarDays, ChevronDown, ChevronRight, ExternalLink, FileText, Filter, ListTree, Network, Search } from 'lucide-react';
import { Dispatch, FormEvent, SetStateAction, useCallback, useEffect, useRef, useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000';
const WORKING_GROUPS = ['RAN2', 'RAN3', 'SA2', 'CT1'];
const GRAPH_NODE_LIMIT = 600;
const NODE_COLORS: Record<string, string> = { company: '#087f72', topic: '#c89018', tdoc: '#cb5a3c', meeting: '#48545d', specification: '#536aa0', release: '#8c5d95', work_item: '#68706e' };

type GraphNode = { id: string; type: string; label: string; title?: string; status?: string };
type GraphEdge = { source: string; target: string; type: string };
type Evidence = { id: string; source_url: string; authority: string; section_path: string[]; excerpt?: string };
type GraphEnvelope = { data: { nodes: GraphNode[]; edges: GraphEdge[]; match_mode: 'all' | 'any' }; dataset_version: string; evidence: Evidence[]; warnings: string[] };
type TDoc = { id: string; meeting_id: string; title: string; source: string; status: string; releases: string[]; specifications: string[]; agenda_item: string; agenda_description: string; revised_from?: string; revised_to?: string; source_url?: string };
type DocumentBlock = { id: string; kind: string; text: string; section_path: string[] };
type DocumentSection = { id: string; parent_id?: string; title: string; depth: number; start_block_index: number; end_block_index: number; direct_block_count: number; descendant_block_count: number; child_count: number };
type TDocEnvelope = { data: { tdoc: TDoc; blocks: DocumentBlock[] } | null; evidence: Evidence[]; completeness: string; warnings: string[]; next_cursor?: string; sections?: DocumentSection[] };

export default function Home() {
  const [query, setQuery] = useState('');
  const [workingGroups, setWorkingGroups] = useState(WORKING_GROUPS);
  const [companies, setCompanies] = useState('');
  const [topics, setTopics] = useState('');
  const [specifications, setSpecifications] = useState('');
  const [matchMode, setMatchMode] = useState<'all' | 'any'>('all');
  const [graph, setGraph] = useState<GraphEnvelope | null>(null);
  const [selected, setSelected] = useState<TDocEnvelope | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const selectDocument = useCallback((id: string) => { void loadDocument(id, setSelected); }, []);

  const loadGraph = useCallback(async () => {
    setLoading(true);
    setError('');
    const parameters = new URLSearchParams({
      query,
      match_mode: matchMode,
      limit: String(GRAPH_NODE_LIMIT),
    });
    workingGroups.forEach((value) => parameters.append('working_groups', value));
    splitValues(companies).forEach((value) => parameters.append('companies', value));
    splitValues(topics).forEach((value) => parameters.append('topics', value));
    splitValues(specifications).forEach((value) => parameters.append('specifications', value));
    try {
      const response = await fetch(`${API_BASE}/api/graph?${parameters.toString()}`);
      if (!response.ok) throw new Error(`Graph request failed with HTTP ${response.status}`);
      const payload = (await response.json()) as GraphEnvelope;
      setGraph(payload);
      const firstTDoc = payload.data.nodes.find((node) => node.type === 'tdoc' && node.status === 'agreed') ?? payload.data.nodes.find((node) => node.type === 'tdoc');
      if (firstTDoc) await loadDocument(firstTDoc.id, setSelected);
      else setSelected(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Graph request failed');
    } finally {
      setLoading(false);
    }
  }, [companies, matchMode, query, specifications, topics, workingGroups]);

  useEffect(() => {
    const timeout = window.setTimeout(() => { void loadGraph(); }, 0);
    return () => window.clearTimeout(timeout);
  }, [loadGraph]);
  function submitSearch(event: FormEvent) { event.preventDefault(); void loadGraph(); }
  function toggleWorkingGroup(group: string) { setWorkingGroups((values) => values.includes(group) ? values.filter((value) => value !== group) : [...values, group]); }

  return <main className="app-shell">
    <header className="topbar">
      <div className="brand"><Network size={19} /><strong>3GPP Evidence Graph</strong></div>
      <form className="global-search" onSubmit={submitSearch}><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="Search the knowledge graph" placeholder="Search TDocs, topics, specs, companies" /><button type="submit" title="Search"><Search size={14} /></button></form>
      <div className="dataset-state"><span />{graph ? `${graph.warnings.length ? 'Preview · ' : ''}${graph.dataset_version}` : 'Loading dataset'}</div>
    </header>
    <div className="workspace">
      <aside className="filters-panel">
        <div className="panel-title"><Filter size={16} /><h2>Filters</h2></div>
        <div className="match-control" aria-label="Filter match mode"><button className={matchMode === 'all' ? 'active' : ''} onClick={() => setMatchMode('all')}>All</button><button className={matchMode === 'any' ? 'active' : ''} onClick={() => setMatchMode('any')}>Any</button></div>
        <div className="filter-block"><span className="filter-label"><Network size={15} />Working group</span><div className="check-grid">{WORKING_GROUPS.map((group) => <label key={group}><input type="checkbox" checked={workingGroups.includes(group)} onChange={() => toggleWorkingGroup(group)} />{group}</label>)}</div></div>
        <FilterInput icon={<Building2 size={15} />} label="Companies" value={companies} onChange={setCompanies} placeholder="Qualcomm, Ericsson" />
        <FilterInput icon={<FileText size={15} />} label="Topics" value={topics} onChange={setTopics} placeholder="Carrier aggregation" />
        <FilterInput icon={<CalendarDays size={15} />} label="Specifications" value={specifications} onChange={setSpecifications} placeholder="38.331" />
        <button className="apply-filters" onClick={() => void loadGraph()}>Apply filters</button>
        <div className="filter-stats"><span>{graph?.data.nodes.filter((node) => node.type === 'tdoc').length ?? 0} TDocs</span><span>{graph?.data.nodes.filter((node) => node.type === 'topic').length ?? 0} topics</span><span>{graph?.data.nodes.filter((node) => node.type === 'company').length ?? 0} companies</span></div>
      </aside>
      <section className="graph-panel" aria-label="3GPP knowledge graph">
        <div className="graph-toolbar"><div><strong>Evidence network</strong><span>{graph?.data.nodes.length ?? 0} of {GRAPH_NODE_LIMIT} projection nodes · {graph?.data.edges.length ?? 0} relationships</span></div><div className="legend"><span className="dot company" />Company<span className="dot topic" />Topic<span className="dot tdoc" />TDoc<span className="dot specification" />Spec</div></div>
        <div className="graph-canvas">{loading && <div className="canvas-state">Loading projection</div>}{error && <div className="canvas-state error">{error}</div>}{graph && !loading && <SigmaGraph data={graph.data} onSelect={selectDocument} />}</div>
      </section>
      <DocumentPanel envelope={selected} onLoadMore={(id, cursor) => { void loadDocument(id, setSelected, { cursor }); }} onJump={(id, startBlock) => { void loadDocument(id, setSelected, { startBlock }); }} />
    </div>
  </main>;
}

function SigmaGraph({ data, onSelect }: { data: GraphEnvelope['data']; onSelect: (id: string) => void }) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!container.current) return;
    let active = true;
    let renderer: { kill: () => void } | null = null;
    const containerElement = container.current;
    async function renderGraph() {
      const [{ default: forceAtlas2 }, { default: Graph }, { default: Sigma }] = await Promise.all([
        import('graphology-layout-forceatlas2'), import('graphology'), import('sigma'),
      ]);
      if (!active) return;
      const graph = new Graph({ multi: true });
      data.nodes.forEach((node, index) => {
        const angle = (index / Math.max(data.nodes.length, 1)) * Math.PI * 2;
        graph.addNode(node.id, { x: Math.cos(angle), y: Math.sin(angle), size: node.type === 'tdoc' ? 11 : node.type === 'topic' ? 9 : 7, color: NODE_COLORS[node.type] ?? '#68706e', label: node.label, nodeType: node.type });
      });
      data.edges.forEach((edge, index) => { if (graph.hasNode(edge.source) && graph.hasNode(edge.target)) graph.addEdgeWithKey(`edge-${index}`, edge.source, edge.target, { size: 1, color: '#aeb7b3', type: 'line' }); });
      if (graph.order > 1) forceAtlas2.assign(graph, { iterations: Math.min(160, 40 + graph.order), settings: forceAtlas2.inferSettings(graph) });
      renderer = new Sigma(graph, containerElement, { allowInvalidContainer: true, labelDensity: 0.12, labelGridCellSize: 110, labelRenderedSizeThreshold: 6, minCameraRatio: 0.12, maxCameraRatio: 8 });
      renderer.on('clickNode', ({ node }) => { if (graph.getNodeAttribute(node, 'nodeType') === 'tdoc') onSelect(node); });
    }
    void renderGraph();
    return () => { active = false; renderer?.kill(); };
  }, [data, onSelect]);
  return <div className="sigma-container" ref={container} />;
}

function DocumentPanel({ envelope, onLoadMore, onJump }: { envelope: TDocEnvelope | null; onLoadMore: (id: string, cursor: string) => void; onJump: (id: string, startBlock: number) => void }) {
  if (!envelope?.data) return <aside className="document-panel empty"><FileText size={20} /><p>No TDoc selected</p></aside>;
  const { tdoc, blocks } = envelope.data;
  return <aside className="document-panel"><div className="doc-heading"><span className={`status ${tdoc.status}`}>{tdoc.status.replaceAll('_', ' ')}</span><span>{tdoc.meeting_id}</span></div><p className="tdoc-id">{tdoc.id}</p><h1>{tdoc.title}</h1><dl className="metadata"><div><dt>Source</dt><dd>{tdoc.source || 'Not recorded'}</dd></div><div><dt>Release</dt><dd>{tdoc.releases.join(', ') || 'Not recorded'}</dd></div><div><dt>Specification</dt><dd>{tdoc.specifications.join(', ') || 'Not recorded'}</dd></div><div><dt>Agenda</dt><dd>{[tdoc.agenda_item, tdoc.agenda_description].filter(Boolean).join(' · ') || 'Not recorded'}</dd></div></dl>{envelope.sections && envelope.sections.length > 1 && <><div className="section-label section-label-icon"><ListTree size={13} />Sections</div><SectionNavigator key={envelope.sections[0].id} sections={envelope.sections} onJump={(startBlock) => onJump(tdoc.id, startBlock)} /></>}<div className="section-label">Document content</div><div className="document-content">{blocks.length ? blocks.map((block) => <section key={block.id} className={`doc-block ${block.kind}`}><small>{block.section_path.join(' / ')}</small><p>{block.text}</p></section>) : <p className="evidence-copy">{envelope.warnings[0] ?? 'Document body is unavailable.'}</p>}{envelope.next_cursor && <button className="load-more" onClick={() => onLoadMore(tdoc.id, envelope.next_cursor!)}>Load more</button>}</div><div className="section-label">Evidence</div>{envelope.evidence.map((evidence) => <a className="citation" key={evidence.id} href={evidence.source_url} target="_blank" rel="noreferrer"><FileText size={15} /><span>{evidence.authority.replaceAll('_', ' ')} · {evidence.section_path.join(' / ') || evidence.id}</span><ExternalLink size={13} /></a>)}{(tdoc.revised_from || tdoc.revised_to) && <><div className="section-label">Revision chain</div><ol className="revision-chain">{tdoc.revised_from && <li>{tdoc.revised_from}</li>}<li>{tdoc.id}</li>{tdoc.revised_to && <li>{tdoc.revised_to}</li>}</ol></>}</aside>;
}

function SectionNavigator({ sections, onJump }: { sections: DocumentSection[]; onJump: (startBlock: number) => void }) {
  const root = sections.find((section) => !section.parent_id);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(root ? [root.id] : []));
  const children = new Map<string, DocumentSection[]>();
  sections.forEach((section) => { if (section.parent_id) children.set(section.parent_id, [...(children.get(section.parent_id) ?? []), section]); });
  const visible: DocumentSection[] = [];
  function append(parentId: string | undefined) { if (!parentId || !expanded.has(parentId)) return; (children.get(parentId) ?? []).forEach((section) => { visible.push(section); append(section.id); }); }
  append(root?.id);
  function toggle(id: string) { setExpanded((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; }); }
  return <nav className="section-index" aria-label="Document sections">{visible.map((section) => <div className="section-row" key={section.id} style={{ paddingLeft: `${Math.min(section.depth - 1, 4) * 12}px` }}>{section.child_count ? <button className="section-toggle" onClick={() => toggle(section.id)} title={expanded.has(section.id) ? `Collapse ${section.title}` : `Expand ${section.title}`}>{expanded.has(section.id) ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</button> : <span className="section-spacer" />}<button className="section-link" onClick={() => onJump(section.start_block_index)} title={`Open ${section.title}`}><span>{section.title}</span><small>{section.descendant_block_count}</small></button></div>)}</nav>;
}

function FilterInput({ icon, label, value, onChange, placeholder }: { icon: React.ReactNode; label: string; value: string; onChange: (value: string) => void; placeholder: string }) { return <label className="filter-block"><span className="filter-label">{icon}{label}</span><input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} /></label>; }
async function loadDocument(id: string, setSelected: Dispatch<SetStateAction<TDocEnvelope | null>>, options: { cursor?: string; startBlock?: number } = {}) { const parameters = new URLSearchParams({ block_limit: '500' }); if (options.cursor) parameters.set('cursor', options.cursor); if (options.startBlock !== undefined) parameters.set('start_block', String(options.startBlock)); const detailRequest = fetch(`${API_BASE}/api/tdocs/${encodeURIComponent(id)}?${parameters.toString()}`); const sectionRequest = options.cursor ? null : fetch(`${API_BASE}/api/tdocs/${encodeURIComponent(id)}/sections?limit=1000`); const [response, sectionResponse] = await Promise.all([detailRequest, sectionRequest]); if (!response.ok) return; const payload = (await response.json()) as TDocEnvelope; const sections = sectionResponse?.ok ? ((await sectionResponse.json()) as { data: DocumentSection[] }).data : undefined; setSelected((current) => options.cursor && current?.data && payload.data && current.data.tdoc.id === payload.data.tdoc.id ? { ...payload, sections: current.sections, data: { ...payload.data, blocks: [...current.data.blocks, ...payload.data.blocks] } } : { ...payload, sections: sections ?? currentSections(current, id) }); }
function currentSections(envelope: TDocEnvelope | null, id: string) { return envelope?.data?.tdoc.id === id ? envelope.sections : undefined; }
function splitValues(value: string) { return value.split(',').map((item) => item.trim()).filter(Boolean); }
