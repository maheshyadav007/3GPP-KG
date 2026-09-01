'use client';

import { Building2, CalendarDays, ChevronDown, ChevronRight, ExternalLink, FileText, Filter, ListTree, Maximize2, Network, RotateCcw, Search, X } from 'lucide-react';
import { CSSProperties, Dispatch, FormEvent, KeyboardEvent, ReactNode, SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type Graph from 'graphology';
import type FA2LayoutSupervisor from 'graphology-layout-forceatlas2/worker';
import type Sigma from 'sigma';

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000';
const MEETING_STORAGE_KEY = 'threegpp-graph-meeting';
const WORKING_GROUP_STORAGE_KEY = 'threegpp-graph-working-group';
const SCOPE_STORAGE_KEY = 'threegpp-graph-scope';
const READER_WIDTH_KEY = 'threegpp-reader-width';
const NODE_COLORS: Record<string, string> = { meeting: '#48545d', tdoc: '#cb5a3c', organization: '#087f72', agenda_item: '#6f7f3f', topic: '#c89018', specification: '#536aa0', release: '#8c5d95', work_item: '#68706e', change_request: '#9a4f68' };
const LEGEND = [['meeting', 'Meeting'], ['organization', 'Company'], ['topic', 'Topic'], ['tdoc', 'TDoc'], ['agenda_item', 'Agenda'], ['specification', 'Spec'], ['release', 'Release'], ['work_item', 'Work item'], ['change_request', 'CR']];

type Meeting = { id: string; working_group_id: string; name: string; starts_on?: string; ends_on?: string; readiness: string; tdoc_count: number };
type ScopeType = 'meeting' | 'working_group';
type GraphNode = { id: string; entity_id: string; type: string; label: string; properties: Record<string, unknown>; boundary: boolean };
type GraphEdge = { id: string; source: string; target: string; type: string; evidence_ids: string[]; highlighted: boolean };
type GraphCounts = { meetings: number; tdocs: number; nodes: number; edges: number; total_tdocs: number; total_nodes: number; total_edges: number };
type RevisionStats = { revision_edges: number; cross_meeting_edges: number; longest_chain: { length: number; tdoc_ids: string[]; meeting_ids: string[] } };
type GraphEnvelope = { data: { scope: { type: ScopeType; id: string; label: string }; meeting?: Meeting; meetings: Meeting[]; nodes: GraphNode[]; edges: GraphEdge[]; counts: GraphCounts; revision_stats: RevisionStats; total_revision_stats: RevisionStats; match_mode: 'all' | 'any' }; dataset_version: string; warnings: string[] };
type FacetKind = 'company' | 'topic' | 'specification';
type FacetOption = { id: string; label: string; tdoc_count: number };
type Evidence = { id: string; source_url: string; authority: string; section_path: string[]; excerpt?: string };
type TDoc = { id: string; meeting_id: string; title: string; source: string; status: string; releases: string[]; specifications: string[]; agenda_item: string; agenda_description: string; revised_from?: string; revised_to?: string; source_url?: string };
type DocumentBlock = { id: string; kind: string; text: string; section_path: string[] };
type DocumentSection = { id: string; parent_id?: string; title: string; depth: number; start_block_index: number; end_block_index: number; direct_block_count: number; descendant_block_count: number; child_count: number };
type TDocEnvelope = { data: { tdoc: TDoc; blocks: DocumentBlock[] } | null; evidence: Evidence[]; completeness: string; warnings: string[]; next_cursor?: string; sections?: DocumentSection[] };

export default function Home() {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [scopeType, setScopeType] = useState<ScopeType>('meeting');
  const [meetingId, setMeetingId] = useState('');
  const [workingGroupId, setWorkingGroupId] = useState('');
  const [query, setQuery] = useState('');
  const [appliedQuery, setAppliedQuery] = useState('');
  const [companies, setCompanies] = useState<FacetOption[]>([]);
  const [topics, setTopics] = useState<FacetOption[]>([]);
  const [specifications, setSpecifications] = useState<FacetOption[]>([]);
  const [matchMode, setMatchMode] = useState<'all' | 'any'>('all');
  const [graph, setGraph] = useState<GraphEnvelope | null>(null);
  const [selected, setSelected] = useState<TDocEnvelope | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [readerWidth, setReaderWidth] = useState(() => typeof window === 'undefined' ? 520 : clampReaderWidth(Number(window.localStorage.getItem(READER_WIDTH_KEY)) || 520));
  const graphRequest = useRef<AbortController | null>(null);
  const documentRequest = useRef<AbortController | null>(null);
  const selectedIdRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    const controller = new AbortController();
    void fetch(`${API_BASE}/api/meetings?limit=100`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Meeting request failed with HTTP ${response.status}`);
        return response.json() as Promise<{ data: Meeting[] }>;
      })
      .then(({ data }) => {
        setMeetings(data);
        const stored = window.localStorage.getItem(MEETING_STORAGE_KEY);
        const restored = data.find((meeting) => meeting.id === stored);
        const fallback = [...data].filter((meeting) => meeting.readiness === 'final_ready' && meeting.ends_on).sort((left, right) => (right.ends_on ?? '').localeCompare(left.ends_on ?? ''))[0] ?? data[0];
        setMeetingId((restored ?? fallback)?.id ?? '');
        const groups = [...new Set(data.map((meeting) => meeting.working_group_id))].sort();
        const storedGroup = window.localStorage.getItem(WORKING_GROUP_STORAGE_KEY);
        setWorkingGroupId(storedGroup && groups.includes(storedGroup) ? storedGroup : (fallback?.working_group_id ?? groups[0] ?? ''));
        setScopeType(window.localStorage.getItem(SCOPE_STORAGE_KEY) === 'working_group' ? 'working_group' : 'meeting');
      })
      .catch((reason: unknown) => { if ((reason as Error).name !== 'AbortError') setError(reason instanceof Error ? reason.message : 'Meeting request failed'); });
    return () => controller.abort();
  }, []);

  const selectedId = selected?.data?.tdoc.id;
  const scopeId = scopeType === 'meeting' ? meetingId : workingGroupId;
  const scopePath = scopeApiPath(scopeType, scopeId);
  useEffect(() => { selectedIdRef.current = selectedId; }, [selectedId]);
  const openDocument = useCallback(async (id: string, options: { cursor?: string; startBlock?: number } = {}) => {
    documentRequest.current?.abort();
    const controller = new AbortController();
    documentRequest.current = controller;
    await loadDocument(id, setSelected, options, controller.signal);
  }, []);
  const loadGraph = useCallback(async () => {
    if (!scopeId) return;
    graphRequest.current?.abort();
    const controller = new AbortController();
    graphRequest.current = controller;
    setLoading(true);
    setError('');
    const parameters = new URLSearchParams({ query: appliedQuery, match_mode: matchMode });
    companies.forEach((value) => parameters.append('company_ids', value.id));
    topics.forEach((value) => parameters.append('topic_ids', value.id));
    specifications.forEach((value) => parameters.append('specification_ids', value.id));
    try {
      const response = await fetch(`${API_BASE}/api/${scopePath}/graph?${parameters}`, { signal: controller.signal });
      if (!response.ok) throw new Error(`Graph request failed with HTTP ${response.status}`);
      const payload = (await response.json()) as GraphEnvelope;
      setGraph(payload);
      const currentId = selectedIdRef.current;
      const currentVisible = currentId && payload.data.nodes.some((node) => node.type === 'tdoc' && node.entity_id === currentId);
      if (!currentVisible) {
        const first = payload.data.nodes.find((node) => node.type === 'tdoc' && node.properties.status === 'agreed') ?? payload.data.nodes.find((node) => node.type === 'tdoc' && !node.boundary);
        if (first) await openDocument(first.entity_id);
        else setSelected(null);
      }
    } catch (reason) {
      if ((reason as Error).name !== 'AbortError') setError(reason instanceof Error ? reason.message : 'Graph request failed');
    } finally {
      if (graphRequest.current === controller) setLoading(false);
    }
  }, [appliedQuery, companies, matchMode, openDocument, scopeId, scopePath, specifications, topics]);

  useEffect(() => { const timeout = window.setTimeout(() => { void loadGraph(); }, 0); return () => { window.clearTimeout(timeout); graphRequest.current?.abort(); }; }, [loadGraph]);
  useEffect(() => () => documentRequest.current?.abort(), []);

  function resetScopeState() {
    documentRequest.current?.abort();
    graphRequest.current?.abort();
    setCompanies([]); setTopics([]); setSpecifications([]); setQuery(''); setAppliedQuery(''); setSelected(null); setGraph(null);
  }
  function changeScopeType(value: ScopeType) {
    resetScopeState();
    window.localStorage.setItem(SCOPE_STORAGE_KEY, value);
    setScopeType(value);
  }
  function changeScopeValue(value: string) {
    resetScopeState();
    if (scopeType === 'meeting') {
      window.localStorage.setItem(MEETING_STORAGE_KEY, value);
      setMeetingId(value);
    } else {
      window.localStorage.setItem(WORKING_GROUP_STORAGE_KEY, value);
      setWorkingGroupId(value);
    }
  }
  function submitSearch(event: FormEvent) { event.preventDefault(); setAppliedQuery(query.trim()); }
  function resizeReader(width: number) { const next = clampReaderWidth(width); setReaderWidth(next); window.localStorage.setItem(READER_WIDTH_KEY, String(next)); }
  const selectDocument = useCallback((id: string) => { void openDocument(id); }, [openDocument]);
  const groupedMeetings = useMemo(() => Object.groupBy(meetings, (meeting) => meeting.working_group_id), [meetings]);
  const workingGroups = useMemo(() => [...new Set(meetings.map((meeting) => meeting.working_group_id))].sort(), [meetings]);
  const workspaceStyle = { '--reader-width': selected?.data ? `${readerWidth}px` : '0px' } as CSSProperties;

  return <main className="app-shell">
    <header className="topbar"><div className="brand"><Network size={19} /><strong>3GPP Evidence Graph</strong></div><form className="global-search" onSubmit={submitSearch}><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="Search the active graph scope" placeholder="Search TDocs in the active scope" /><button type="submit" title="Search"><Search size={14} /></button></form><div className="dataset-state"><span />{graph ? `${graph.warnings.length ? 'Preview · ' : ''}${graph.dataset_version}` : 'Loading dataset'}</div></header>
    <div className="workspace" style={workspaceStyle}>
      <aside className="filters-panel"><div className="panel-title"><Filter size={16} /><h2>Filters</h2></div><div className="match-control scope-control" aria-label="Graph scope"><button className={scopeType === 'meeting' ? 'active' : ''} onClick={() => changeScopeType('meeting')}>Meeting</button><button className={scopeType === 'working_group' ? 'active' : ''} onClick={() => changeScopeType('working_group')}>Working group</button></div><label className="filter-block meeting-filter"><span className="filter-label"><CalendarDays size={15} />{scopeType === 'meeting' ? 'Meeting' : 'Working group'}</span>{scopeType === 'meeting' ? <select value={meetingId} onChange={(event) => changeScopeValue(event.target.value)} aria-label="Active meeting">{Object.entries(groupedMeetings).map(([group, values]) => <optgroup key={group} label={group}>{values?.map((meeting) => <option value={meeting.id} key={meeting.id}>{meeting.id} · {meeting.tdoc_count.toLocaleString()} TDocs</option>)}</optgroup>)}</select> : <select value={workingGroupId} onChange={(event) => changeScopeValue(event.target.value)} aria-label="Active working group">{workingGroups.map((group) => <option value={group} key={group}>{group} · {meetings.filter((meeting) => meeting.working_group_id === group).length} meetings</option>)}</select>}</label><div className="match-control" aria-label="Filter match mode"><button className={matchMode === 'all' ? 'active' : ''} onClick={() => setMatchMode('all')}>All</button><button className={matchMode === 'any' ? 'active' : ''} onClick={() => setMatchMode('any')}>Any</button></div><FacetCombobox scopePath={scopePath} kind="company" icon={<Building2 size={15} />} label="Companies" selected={companies} onChange={setCompanies} /><FacetCombobox scopePath={scopePath} kind="topic" icon={<FileText size={15} />} label="Topics" selected={topics} onChange={setTopics} /><FacetCombobox scopePath={scopePath} kind="specification" icon={<ListTree size={15} />} label="Specifications" selected={specifications} onChange={setSpecifications} /><div className="filter-stats"><span>{graph?.data.counts.meetings.toLocaleString() ?? 0} meetings</span><span>{graph?.data.counts.tdocs.toLocaleString() ?? 0} TDocs</span><span>{graph?.data.counts.nodes.toLocaleString() ?? 0} nodes</span><span>{graph?.data.counts.edges.toLocaleString() ?? 0} edges</span>{graph && <><span>{graph.data.revision_stats.cross_meeting_edges.toLocaleString()} cross-meeting revisions</span><span>Longest chain: {graph.data.revision_stats.longest_chain.length.toLocaleString()}</span></>}</div></aside>
      <section className="graph-panel" aria-label="3GPP knowledge graph"><div className="graph-toolbar"><div><strong>Complete {scopeType === 'meeting' ? 'meeting' : `${workingGroupId} working group`} graph</strong><span>{graph ? graphCountLabel(graph.data.counts) : 'Loading graph scope'}</span>{graph && <span>{revisionSummary(graph.data.revision_stats)}</span>}</div><div className="legend">{LEGEND.map(([type, label]) => <span className="legend-item" key={type}><i style={{ background: NODE_COLORS[type] }} />{label}</span>)}</div></div><div className="graph-canvas">{loading && <div className="canvas-state">Loading complete graph</div>}{error && <div className="canvas-state error">{error}</div>}{graph && !loading && <SigmaGraph data={graph.data} onSelect={selectDocument} />}</div></section>
      {selected?.data && <DocumentPanel envelope={selected} onClose={() => { documentRequest.current?.abort(); setSelected(null); }} onResize={resizeReader} onLoadMore={(id, cursor) => { void openDocument(id, { cursor }); }} onJump={(id, startBlock) => { void openDocument(id, { startBlock }); }} />}
    </div>
  </main>;
}

export function FacetCombobox({ scopePath, kind, icon, label, selected, onChange }: { scopePath: string; kind: FacetKind; icon: ReactNode; label: string; selected: FacetOption[]; onChange: (values: FacetOption[]) => void }) {
  const [input, setInput] = useState(''); const [options, setOptions] = useState<FacetOption[]>([]); const [open, setOpen] = useState(false); const [active, setActive] = useState(0); const request = useRef<AbortController | null>(null);
  useEffect(() => { if (!open || !scopePath) return; const timeout = window.setTimeout(() => { request.current?.abort(); const controller = new AbortController(); request.current = controller; void fetch(`${API_BASE}/api/${scopePath}/facets/${kind}?q=${encodeURIComponent(input)}&limit=20`, { signal: controller.signal }).then((response) => response.ok ? response.json() : Promise.reject(new Error(`Facet request failed with HTTP ${response.status}`))).then((payload: { data: FacetOption[] }) => { setOptions(payload.data.filter((option) => !selected.some((value) => value.id === option.id))); setActive(0); }).catch((reason: Error) => { if (reason.name !== 'AbortError') setOptions([]); }); }, 180); return () => window.clearTimeout(timeout); }, [input, kind, open, scopePath, selected]);
  useEffect(() => () => request.current?.abort(), []);
  function choose(option: FacetOption) { onChange([...selected, option]); setInput(''); setOpen(true); }
  function keyDown(event: KeyboardEvent<HTMLInputElement>) { if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true); setActive((value) => Math.min(value + 1, options.length - 1)); } if (event.key === 'ArrowUp') { event.preventDefault(); setActive((value) => Math.max(value - 1, 0)); } if (event.key === 'Enter' && open && options[active]) { event.preventDefault(); choose(options[active]); } if (event.key === 'Escape') setOpen(false); if (event.key === 'Backspace' && !input && selected.length) onChange(selected.slice(0, -1)); }
  return <div className="filter-block facet-field"><span className="filter-label">{icon}{label}</span><div className="combobox-shell">{selected.map((option) => <span className="filter-chip" key={option.id}>{option.label}<button title={`Remove ${option.label}`} onClick={() => onChange(selected.filter((value) => value.id !== option.id))}><X size={11} /></button></span>)}<input value={input} disabled={!scopePath} onChange={(event) => { setInput(event.target.value); setOpen(true); }} onFocus={() => setOpen(true)} onBlur={() => window.setTimeout(() => setOpen(false), 120)} onKeyDown={keyDown} role="combobox" aria-expanded={open} aria-controls={`${kind}-options`} aria-autocomplete="list" placeholder={selected.length ? 'Add another' : `Find ${label.toLowerCase()}`} /></div>{open && <div className="facet-options" id={`${kind}-options`} role="listbox">{options.length ? options.map((option, index) => <button role="option" aria-selected={index === active} className={index === active ? 'active' : ''} key={option.id} onMouseDown={(event) => event.preventDefault()} onClick={() => choose(option)}><span>{option.label}</span><small>{option.tdoc_count.toLocaleString()}</small></button>) : <p>No matching values</p>}</div>}</div>;
}

function SigmaGraph({ data, onSelect }: { data: GraphEnvelope['data']; onSelect: (id: string) => void }) {
  const container = useRef<HTMLDivElement>(null); const rendererRef = useRef<Sigma | null>(null); const graphRef = useRef<Graph | null>(null);
  useEffect(() => { if (!container.current || !data.nodes.length) return; let active = true; let renderer: Sigma | null = null; let layout: FA2LayoutSupervisor | null = null; let stopTimer = 0; const containerElement = container.current; async function renderGraph() { const [{ default: forceAtlas2 }, { default: FA2Layout }, { default: GraphClass }, { default: SigmaClass }] = await Promise.all([import('graphology-layout-forceatlas2'), import('graphology-layout-forceatlas2/worker'), import('graphology'), import('sigma')]); if (!active) return; const graph = new GraphClass({ multi: true }); data.nodes.forEach((node, index) => { const angle = hashNumber(node.id) * Math.PI * 2; const radius = 1 + (index % 29) / 20; const longestRevision = node.properties.longest_revision_chain === true; graph.addNode(node.id, { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius, size: longestRevision ? 8 : node.type === 'tdoc' ? 5 : node.type === 'meeting' ? 12 : node.type === 'topic' ? 7 : 4, color: node.boundary ? '#ffffff' : longestRevision ? '#d23f2f' : NODE_COLORS[node.type] ?? '#68706e', label: node.label, nodeType: node.type, entityId: node.entity_id, highlighted: node.boundary || longestRevision }); }); data.edges.forEach((edge) => { if (graph.hasNode(edge.source) && graph.hasNode(edge.target)) graph.addEdgeWithKey(edge.id, edge.source, edge.target, { size: edge.highlighted ? 2.2 : edge.type === 'revises' ? 1.2 : 0.55, color: edge.highlighted ? '#d23f2f' : edge.type === 'revises' ? '#d98972' : '#aeb7b3', type: 'line', zIndex: edge.highlighted ? 2 : 0 }); }); renderer = new SigmaClass(graph, containerElement, { allowInvalidContainer: true, hideEdgesOnMove: true, labelDensity: 0.08, labelGridCellSize: 140, labelRenderedSizeThreshold: 7, minCameraRatio: 0.03, maxCameraRatio: 15, zIndex: true }); rendererRef.current = renderer; graphRef.current = graph; renderer.on('clickNode', ({ node }: { node: string }) => { if (graph.getNodeAttribute(node, 'nodeType') === 'tdoc') onSelect(String(graph.getNodeAttribute(node, 'entityId'))); }); if (graph.order > 1) { layout = new FA2Layout(graph, { settings: { ...forceAtlas2.inferSettings(graph), barnesHutOptimize: true, gravity: 1.2, scalingRatio: 8, slowDown: 5 } }); layout.start(); stopTimer = window.setTimeout(() => { layout?.stop(); renderer?.refresh(); renderer?.getCamera().animatedReset({ duration: 400 }); }, Math.min(6000, 1800 + graph.order)); } } void renderGraph(); return () => { active = false; window.clearTimeout(stopTimer); layout?.kill(); renderer?.kill(); rendererRef.current = null; graphRef.current = null; }; }, [data, onSelect]);
  function resetLayout() { graphRef.current?.forEachNode((node: string, attributes: Record<string, unknown>) => { const angle = hashNumber(node) * Math.PI * 2; graphRef.current?.setNodeAttribute(node, 'x', Math.cos(angle) * Number(attributes.size ?? 1)); graphRef.current?.setNodeAttribute(node, 'y', Math.sin(angle) * Number(attributes.size ?? 1)); }); rendererRef.current?.refresh(); rendererRef.current?.getCamera().animatedReset({ duration: 300 }); }
  return <><div className="sigma-container" ref={container} /><div className="graph-controls"><button title="Fit graph" onClick={() => rendererRef.current?.getCamera().animatedReset({ duration: 300 })}><Maximize2 size={16} /></button><button title="Reset positions" onClick={resetLayout}><RotateCcw size={15} /></button></div></>;
}

function DocumentPanel({ envelope, onClose, onResize, onLoadMore, onJump }: { envelope: TDocEnvelope; onClose: () => void; onResize: (width: number) => void; onLoadMore: (id: string, cursor: string) => void; onJump: (id: string, startBlock: number) => void }) {
  if (!envelope.data) return null; const { tdoc, blocks } = envelope.data;
  function startResize(event: React.PointerEvent<HTMLButtonElement>) { event.currentTarget.setPointerCapture(event.pointerId); function move(pointer: PointerEvent) { onResize(window.innerWidth - pointer.clientX); } function stop() { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', stop); } window.addEventListener('pointermove', move); window.addEventListener('pointerup', stop); }
  return <aside className="document-panel"><button className="reader-resize" title="Resize document reader" onPointerDown={startResize} /><button className="reader-close" title="Close document reader" onClick={onClose}><X size={16} /></button><div className="doc-heading"><span className={`status ${tdoc.status}`}>{tdoc.status.replaceAll('_', ' ')}</span><span>{tdoc.meeting_id}</span></div><p className="tdoc-id">{tdoc.id}</p><h1>{tdoc.title}</h1><dl className="metadata"><div><dt>Source</dt><dd>{tdoc.source || 'Not recorded'}</dd></div><div><dt>Release</dt><dd>{tdoc.releases.join(', ') || 'Not recorded'}</dd></div><div><dt>Specification</dt><dd>{tdoc.specifications.join(', ') || 'Not recorded'}</dd></div><div><dt>Agenda</dt><dd>{[tdoc.agenda_item, tdoc.agenda_description].filter(Boolean).join(' · ') || 'Not recorded'}</dd></div></dl>{envelope.sections && envelope.sections.length > 1 && <><div className="section-label section-label-icon"><ListTree size={13} />Sections</div><SectionNavigator key={envelope.sections[0].id} sections={envelope.sections} onJump={(startBlock) => onJump(tdoc.id, startBlock)} /></>}<div className="section-label">Document content</div><div className="document-content">{blocks.length ? blocks.map((block) => <section key={block.id} className={`doc-block ${block.kind}`}><small>{block.section_path.join(' / ')}</small><p>{block.text}</p></section>) : <p className="evidence-copy">{envelope.warnings[0] ?? 'Document body is unavailable.'}</p>}{envelope.next_cursor && <button className="load-more" onClick={() => onLoadMore(tdoc.id, envelope.next_cursor!)}>Load more</button>}</div><div className="section-label">Evidence</div>{envelope.evidence.map((evidence) => <a className="citation" key={evidence.id} href={evidence.source_url} target="_blank" rel="noreferrer"><FileText size={15} /><span>{evidence.authority.replaceAll('_', ' ')} · {evidence.section_path.join(' / ') || evidence.id}</span><ExternalLink size={13} /></a>)}{(tdoc.revised_from || tdoc.revised_to) && <><div className="section-label">Revision chain</div><ol className="revision-chain">{tdoc.revised_from && <li>{tdoc.revised_from}</li>}<li>{tdoc.id}</li>{tdoc.revised_to && <li>{tdoc.revised_to}</li>}</ol></>}</aside>;
}

function SectionNavigator({ sections, onJump }: { sections: DocumentSection[]; onJump: (startBlock: number) => void }) { const root = sections.find((section) => !section.parent_id); const [expanded, setExpanded] = useState<Set<string>>(() => new Set(root ? [root.id] : [])); const children = new Map<string, DocumentSection[]>(); sections.forEach((section) => { if (section.parent_id) children.set(section.parent_id, [...(children.get(section.parent_id) ?? []), section]); }); const visible: DocumentSection[] = []; function append(parentId: string | undefined) { if (!parentId || !expanded.has(parentId)) return; (children.get(parentId) ?? []).forEach((section) => { visible.push(section); append(section.id); }); } append(root?.id); function toggle(id: string) { setExpanded((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; }); } return <nav className="section-index" aria-label="Document sections">{visible.map((section) => <div className="section-row" key={section.id} style={{ paddingLeft: `${Math.min(section.depth - 1, 4) * 12}px` }}>{section.child_count ? <button className="section-toggle" onClick={() => toggle(section.id)} title={expanded.has(section.id) ? `Collapse ${section.title}` : `Expand ${section.title}`}>{expanded.has(section.id) ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</button> : <span className="section-spacer" />}<button className="section-link" onClick={() => onJump(section.start_block_index)} title={`Open ${section.title}`}><span>{section.title}</span><small>{section.descendant_block_count}</small></button></div>)}</nav>; }

async function loadDocument(id: string, setSelected: Dispatch<SetStateAction<TDocEnvelope | null>>, options: { cursor?: string; startBlock?: number } = {}, signal?: AbortSignal) { const parameters = new URLSearchParams({ block_limit: '500' }); if (options.cursor) parameters.set('cursor', options.cursor); if (options.startBlock !== undefined) parameters.set('start_block', String(options.startBlock)); const requestOptions = signal ? { signal } : undefined; const detailRequest = fetch(`${API_BASE}/api/tdocs/${encodeURIComponent(id)}?${parameters}`, requestOptions); const sectionRequest = options.cursor ? null : fetch(`${API_BASE}/api/tdocs/${encodeURIComponent(id)}/sections?limit=1000`, requestOptions); try { const [response, sectionResponse] = await Promise.all([detailRequest, sectionRequest]); if (!response.ok) return; const payload = (await response.json()) as TDocEnvelope; const sections = sectionResponse?.ok ? ((await sectionResponse.json()) as { data: DocumentSection[] }).data : undefined; setSelected((current) => options.cursor && current?.data && payload.data && current.data.tdoc.id === payload.data.tdoc.id ? { ...payload, sections: current.sections, data: { ...payload.data, blocks: [...current.data.blocks, ...payload.data.blocks] } } : { ...payload, sections: sections ?? (current?.data?.tdoc.id === id ? current.sections : undefined) }); } catch (reason) { if ((reason as Error).name !== 'AbortError') throw reason; } }
export function graphCountLabel(counts: GraphCounts) { const filtered = counts.tdocs !== counts.total_tdocs; return filtered ? `${counts.nodes.toLocaleString()} of ${counts.total_nodes.toLocaleString()} nodes · ${counts.edges.toLocaleString()} of ${counts.total_edges.toLocaleString()} relationships` : `${counts.nodes.toLocaleString()} nodes · ${counts.edges.toLocaleString()} relationships · no truncation`; }
export function scopeApiPath(scopeType: ScopeType, scopeId: string) { return `${scopeType === 'meeting' ? 'meetings' : 'working-groups'}/${encodeURIComponent(scopeId)}`; }
export function revisionSummary(stats: RevisionStats) { return `${stats.revision_edges.toLocaleString()} revision links · ${stats.cross_meeting_edges.toLocaleString()} cross meeting · longest chain ${stats.longest_chain.length.toLocaleString()}`; }
export function clampReaderWidth(width: number) { return Math.round(Math.max(420, Math.min(window.innerWidth * 0.5, width))); }
function hashNumber(value: string) { let hash = 2166136261; for (let index = 0; index < value.length; index += 1) hash = Math.imul(hash ^ value.charCodeAt(index), 16777619); return (hash >>> 0) / 4294967295; }
