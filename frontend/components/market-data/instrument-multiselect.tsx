"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { useInstruments } from "@/lib/hooks";
import type { InstrumentOut } from "@/lib/types";

const EXCHANGES = [
  { value: "", label: "All Markets" },
  { value: "NSE", label: "NSE Markets" },
  { value: "DELTA", label: "Delta Markets" },
];

const SELECT_ALL_LIMIT = 3000;
const SEARCH_LIMIT = 200;

interface InstrumentMultiSelectProps {
  value: InstrumentOut[];
  onChange: (next: InstrumentOut[]) => void;
  className?: string;
}

/** Exchange-filtered, checkbox-driven bulk instrument picker over the main
 * instrument catalog -- reused wherever a page needs to select many
 * instruments at once (Watchlists bulk-add, Strategy Builder, Backtesting,
 * Optimization). Every symbol is a hyperlink straight to its chart. */
export function InstrumentMultiSelect({ value, onChange, className }: InstrumentMultiSelectProps) {
  const [q, setQ] = useState("");
  const [exchange, setExchange] = useState("");
  const limit = q ? SEARCH_LIMIT : SELECT_ALL_LIMIT;
  const { data: instruments, isLoading } = useInstruments(q, exchange || undefined, limit);

  const selectedIds = useMemo(() => new Set(value.map((i) => i.id)), [value]);

  function toggle(instrument: InstrumentOut) {
    if (selectedIds.has(instrument.id)) {
      onChange(value.filter((i) => i.id !== instrument.id));
    } else {
      onChange([...value, instrument]);
    }
  }

  const allFilteredSelected = !!instruments?.length && instruments.every((i) => selectedIds.has(i.id));

  function toggleSelectAll() {
    if (!instruments?.length) return;
    if (allFilteredSelected) {
      const filteredIds = new Set(instruments.map((i) => i.id));
      onChange(value.filter((i) => !filteredIds.has(i.id)));
    } else {
      const merged = new Map(value.map((i) => [i.id, i] as const));
      for (const i of instruments) merged.set(i.id, i);
      onChange([...merged.values()]);
    }
  }

  return (
    <div className={className}>
      <div className="flex flex-wrap items-center gap-2">
        <Input placeholder="Search symbol or name..." value={q} onChange={(e) => setQ(e.target.value)} className="max-w-xs" />
        <Select value={exchange} onChange={(e) => setExchange(e.target.value)} className="w-40">
          {EXCHANGES.map((e) => (
            <option key={e.value} value={e.value}>{e.label}</option>
          ))}
        </Select>
        {value.length > 0 && (
          <button type="button" onClick={() => onChange([])} className="text-xs text-text-muted hover:text-text-primary">
            Clear {value.length} selected
          </button>
        )}
      </div>

      <div className="mt-2 max-h-64 overflow-y-auto rounded-md border border-border">
        {isLoading ? (
          <p className="p-2 text-xs text-text-muted">Loading...</p>
        ) : !instruments?.length ? (
          <p className="p-2 text-xs text-text-muted">No instruments match.</p>
        ) : (
          <>
            <label className="flex items-center gap-2 border-b border-border bg-surface-elevated px-2 py-1.5 text-xs font-medium text-text-secondary">
              <input type="checkbox" checked={allFilteredSelected} onChange={toggleSelectAll} />
              Select all ({instruments.length}
              {instruments.length >= limit ? "+" : ""})
            </label>
            {instruments.map((i) => (
              <label key={i.id} className="flex items-center gap-2 px-2 py-1.5 text-sm hover:bg-surface-elevated">
                <input type="checkbox" checked={selectedIds.has(i.id)} onChange={() => toggle(i)} />
                <span className="w-14 shrink-0 text-[10px] uppercase text-text-muted">{i.exchange}</span>
                <Link
                  href={`/charts?instrument_id=${i.id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  className="shrink-0 font-medium text-active underline underline-offset-2 hover:opacity-80"
                >
                  {i.symbol}
                </Link>
                <span className="truncate text-text-muted">{i.name}</span>
              </label>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
