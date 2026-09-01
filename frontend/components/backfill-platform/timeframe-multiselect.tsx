"use client";

import { ChevronDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { TimeframeOptionOut } from "@/lib/types";

interface TimeframeMultiSelectProps {
  options: TimeframeOptionOut[] | undefined;
  value: string[];
  onChange: (next: string[]) => void;
}

/** A checkbox-driven dropdown so one Backfill click can queue every checked
 * timeframe at once, instead of picking one timeframe, clicking Backfill,
 * switching the dropdown, clicking Backfill again, and so on. */
export function TimeframeMultiSelect({ options, value, onChange }: TimeframeMultiSelectProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [open]);

  function toggle(tf: string) {
    if (value.includes(tf)) {
      if (value.length === 1) return; // always keep at least one selected
      onChange(value.filter((v) => v !== tf));
    } else {
      onChange([...value, tf]);
    }
  }

  const allSelected = !!options?.length && options.every((tf) => value.includes(tf.value));

  function toggleSelectAll() {
    if (!options?.length) return;
    onChange(allSelected ? [options[0].value] : options.map((tf) => tf.value));
  }

  const summary = value.length === 1 ? value[0] : value.length === 0 ? "Select..." : `${value.length} timeframes (${value.join(", ")})`;

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between rounded-md border border-border bg-surface px-3 py-2 text-left text-sm text-text-primary hover:border-active/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
        title={value.length > 1 ? value.join(", ") : undefined}
      >
        <span className="truncate">{summary}</span>
        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-text-muted" />
      </button>
      {open && (
        <div className="absolute z-20 mt-1 w-full min-w-[10rem] rounded-md border border-border bg-surface-elevated p-1 shadow-lg">
          {!options?.length ? (
            <p className="px-2 py-1.5 text-xs text-text-muted">Loading...</p>
          ) : (
            <>
              <label className="flex items-center gap-2 rounded border-b border-border px-2 py-1.5 text-sm font-medium text-text-secondary hover:bg-surface">
                <input type="checkbox" checked={allSelected} onChange={toggleSelectAll} />
                Select all
              </label>
              {options.map((tf) => (
                <label key={tf.value} className="flex items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-surface">
                  <input type="checkbox" checked={value.includes(tf.value)} onChange={() => toggle(tf.value)} />
                  {tf.value}
                  {!tf.native && <span className="text-xs text-text-muted">(derived)</span>}
                </label>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
