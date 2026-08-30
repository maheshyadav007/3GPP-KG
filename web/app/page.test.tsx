import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FacetCombobox, clampReaderWidth, graphCountLabel } from './page';

describe('meeting graph controls', () => {
  afterEach(cleanup);
  beforeEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1400 });
  });

  it('loads meeting-scoped suggestions and selects an exact option', async () => {
    const onChange = vi.fn();
    const request = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: [{ id: 'ericsson', label: 'Ericsson', tdoc_count: 12 }] }))
    );
    render(<FacetCombobox meetingId="RAN2-135" kind="company" icon={null} label="Companies" selected={[]} onChange={onChange} />);
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
    render(<FacetCombobox meetingId="RAN2-135" kind="company" icon={null} label="Companies" selected={[{ id: 'ericsson', label: 'Ericsson', tdoc_count: 12 }]} onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Backspace' });
    expect(onChange).toHaveBeenCalledWith([]);
    expect(graphCountLabel({ tdocs: 3, nodes: 8, edges: 9, total_tdocs: 3, total_nodes: 8, total_edges: 9 })).toContain('no truncation');
    expect(graphCountLabel({ tdocs: 1, nodes: 4, edges: 3, total_tdocs: 3, total_nodes: 8, total_edges: 9 })).toContain('4 of 8 nodes');
  });

  it('clamps the persisted reader width', async () => {
    expect(clampReaderWidth(100)).toBe(420);
    expect(clampReaderWidth(900)).toBe(700);
    await waitFor(() => expect(true).toBe(true));
  });
});
