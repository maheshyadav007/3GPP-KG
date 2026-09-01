import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import Home, { FacetCombobox, clampReaderWidth, graphCountLabel, revisionSummary, scopeApiPath } from './page';

describe('meeting graph controls', () => {
  afterEach(cleanup);
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1400 });
  });

  it('loads meeting-scoped suggestions and selects an exact option', async () => {
    const onChange = vi.fn();
    const request = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: [{ id: 'ericsson', label: 'Ericsson', tdoc_count: 12 }] }))
    );
    render(<FacetCombobox scopePath="meetings/RAN2-135" kind="company" icon={null} label="Companies" selected={[]} onChange={onChange} />);
    const input = screen.getByRole('combobox');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'eri' } });
    expect(await screen.findByRole('option', { name: /Ericsson/ })).toBeVisible();
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining('/api/meetings/RAN2-135/facets/company?q=eri'),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith([{ id: 'ericsson', label: 'Ericsson', tdoc_count: 12 }]);
  });

  it('removes the last chip with backspace and reports complete versus filtered counts', () => {
    const onChange = vi.fn();
    render(<FacetCombobox scopePath="meetings/RAN2-135" kind="company" icon={null} label="Companies" selected={[{ id: 'ericsson', label: 'Ericsson', tdoc_count: 12 }]} onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Backspace' });
    expect(onChange).toHaveBeenCalledWith([]);
    expect(graphCountLabel({ meetings: 1, tdocs: 3, nodes: 8, edges: 9, total_tdocs: 3, total_nodes: 8, total_edges: 9 })).toContain('no truncation');
    expect(graphCountLabel({ meetings: 1, tdocs: 1, nodes: 4, edges: 3, total_tdocs: 3, total_nodes: 8, total_edges: 9 })).toContain('4 of 8 nodes');
  });

  it('clamps the persisted reader width', async () => {
    expect(clampReaderWidth(100)).toBe(420);
    expect(clampReaderWidth(900)).toBe(700);
    await waitFor(() => expect(true).toBe(true));
  });

  it('builds WG scope paths and summarizes cross-meeting revisions', () => {
    expect(scopeApiPath('working_group', 'RAN2')).toBe('working-groups/RAN2');
    expect(revisionSummary({ revision_edges: 138, cross_meeting_edges: 41, longest_chain: { length: 7, tdoc_ids: [], meeting_ids: [] } })).toBe('138 revision links · 41 cross meeting · longest chain 7');
  });

  it('switches from a meeting graph to the complete working-group graph', async () => {
    const graphData = (scope: 'meeting' | 'working_group') => ({
      data: {
        scope: { type: scope, id: scope === 'meeting' ? 'RAN2-135' : 'RAN2', label: 'RAN2' },
        meetings: [], nodes: [], edges: [],
        counts: { meetings: scope === 'meeting' ? 1 : 5, tdocs: 0, nodes: 0, edges: 0, total_tdocs: 0, total_nodes: 0, total_edges: 0 },
        revision_stats: { revision_edges: 0, cross_meeting_edges: 0, longest_chain: { length: 0, tdoc_ids: [], meeting_ids: [] } },
        total_revision_stats: { revision_edges: 0, cross_meeting_edges: 0, longest_chain: { length: 0, tdoc_ids: [], meeting_ids: [] } },
        match_mode: 'all',
      }, dataset_version: 'test-v1', warnings: [],
    });
    const request = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('/api/meetings?')) return new Response(JSON.stringify({ data: [{ id: 'RAN2-135', working_group_id: 'RAN2', name: 'RAN2 #135', ends_on: '2026-08-28', readiness: 'final_ready', tdoc_count: 1341 }] }));
      if (url.includes('/api/working-groups/RAN2/graph')) return new Response(JSON.stringify(graphData('working_group')));
      return new Response(JSON.stringify(graphData('meeting')));
    });
    render(<Home />);
    expect(await screen.findByRole('combobox', { name: 'Active meeting' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Working group' }));
    expect(await screen.findByRole('combobox', { name: 'Active working group' })).toBeVisible();
    await waitFor(() => expect(request).toHaveBeenCalledWith(
      expect.stringContaining('/api/working-groups/RAN2/graph'),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
  });
});
