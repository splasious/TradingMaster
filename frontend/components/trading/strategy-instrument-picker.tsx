"use client";

import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { marketLabel } from "@/lib/market";
import type { InstrumentOut } from "@/lib/types";

/** A strategy built in Strategy Builder already names the instrument(s) it
 * was designed and validated against (StrategyVersion.instrument_ids) --
 * re-asking for one via a blind global search elsewhere, unrelated to what
 * the strategy was actually built for, is unnecessary friction. When the
 * strategy has its own list, offer that list directly (checkboxes,
 * defaulting to all selected) and fire one deployment per checked
 * instrument. Shared by Paper Trading and Live Trading's Start Deployment
 * modals. */
export function StrategyInstrumentPicker({
  strategyVersionInstrumentIds,
  selectedIds,
  onChange,
}: {
  strategyVersionInstrumentIds: string[];
  selectedIds: Set<string>;
  onChange: (ids: Set<string>) => void;
}) {
  const [instruments, setInstruments] = useState<InstrumentOut[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all(strategyVersionInstrumentIds.map((id) => apiFetch<InstrumentOut>(`/api/v1/instruments/${id}`).catch(() => null))).then(
      (results) => {
        if (cancelled) return;
        const loaded = results.filter((r): r is InstrumentOut => r !== null);
        setInstruments(loaded);
        onChange(new Set(loaded.map((i) => i.id)));
      },
    );
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runs once per mount; the parent remounts this (fresh state) via a key when the strategy changes
  }, []);

  function toggle(id: string) {
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(next);
  }

  if (!instruments) {
    return <p className="text-sm text-text-muted">Loading this strategy&apos;s instruments...</p>;
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <label className="text-sm font-medium text-text-secondary">
          Instruments ({selectedIds.size}/{instruments.length} selected)
        </label>
        <button
          type="button"
          className="text-xs text-active hover:underline"
          onClick={() => onChange(selectedIds.size === instruments.length ? new Set() : new Set(instruments.map((i) => i.id)))}
        >
          {selectedIds.size === instruments.length ? "Deselect all" : "Select all"}
        </button>
      </div>
      <div className="max-h-48 space-y-0.5 overflow-y-auto rounded-md border border-border p-1.5">
        {instruments.map((i) => (
          <label key={i.id} className="flex items-center gap-2 rounded px-1.5 py-1 text-sm text-text-secondary hover:bg-surface-elevated">
            <input type="checkbox" checked={selectedIds.has(i.id)} onChange={() => toggle(i.id)} />
            {i.symbol} <span className="text-text-muted">({marketLabel(i.exchange)})</span>
          </label>
        ))}
      </div>
    </div>
  );
}
