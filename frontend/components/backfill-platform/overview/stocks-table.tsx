"use client";

import { ChevronLeft, ChevronRight, Search } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useBfStocks } from "@/lib/hooks";
import type { BfStockCell, BfStockFilter, BfTimeframe } from "@/lib/types";
import { cn } from "@/lib/utils";

import { fmtLongDay, fmtShortDay, TIMEFRAME_SHORT } from "./format";
import { FreshInline, type FreshKind } from "./fresh-status";

const TIMEFRAMES: BfTimeframe[] = ["1m", "5m", "15m", "30m", "60m", "1d"];
const PAGE_SIZE = 25;
const FILTERS: { key: BfStockFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "current", label: "Fully current" },
  { key: "behind", label: "Behind on a timeframe" },
  { key: "partial", label: "Partial days" },
  { key: "failed", label: "Failed" },
];

function Cell({ cell }: { cell?: BfStockCell }) {
  if (!cell) return <FreshInline kind="none" label="not tracked" />;
  const kind = cell.status as FreshKind;
  const label = kind === "updating" ? "updating" : kind === "expired" ? `expired · ${fmtShortDay(cell.saved_up_to)}` : fmtShortDay(cell.saved_up_to);
  return (
    <span title={cell.sessions_behind ? `${cell.sessions_behind} session${cell.sessions_behind === 1 ? "" : "s"} behind` : undefined}>
      <FreshInline kind={kind} label={cell.partial ? `${label} · partial` : label} />
    </span>
  );
}

export function StocksTable() {
  const [source, setSource] = useState<"zerodha" | "zerodha_nfo">("zerodha");
  const [filter, setFilter] = useState<BfStockFilter>("all");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);
  const { data, isLoading } = useBfStocks(source, filter, q, page, PAGE_SIZE);
  const unit = source === "zerodha" ? "Stocks" : "Contracts";
  const first = page * PAGE_SIZE;

  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 pb-2.5 pt-3.5">
        <div className="flex flex-wrap items-center gap-3">
          <h3 className="text-sm font-semibold text-text-primary">{unit}</h3>
          <div className="flex flex-wrap gap-1.5">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                type="button"
                aria-pressed={filter === f.key}
                onClick={() => {
                  setFilter(f.key);
                  setPage(0);
                }}
                className={cn(
                  "rounded-full border px-2.5 py-0.5 text-xs",
                  filter === f.key ? "border-text-primary bg-text-primary text-surface" : "border-border text-text-secondary hover:border-border-strong",
                )}
              >
                {f.label} <span className="font-financial">{data ? data.counts[f.key].toLocaleString("en-IN") : ""}</span>
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="inline-flex rounded-lg border border-border p-0.5">
            {(["zerodha", "zerodha_nfo"] as const).map((s) => (
              <button
                key={s}
                type="button"
                aria-pressed={source === s}
                onClick={() => {
                  setSource(s);
                  setPage(0);
                }}
                className={cn("rounded-md px-3 py-1 text-[13px]", source === s ? "bg-active-soft font-medium text-active" : "text-text-secondary")}
              >
                {s === "zerodha" ? "NSE Equity" : "NFO"}
              </button>
            ))}
          </div>
          <label className="flex h-8 w-56 items-center gap-2 rounded-md border border-border-strong bg-surface px-2.5 text-[13px]">
            <Search className="h-3.5 w-3.5 text-text-muted" aria-hidden />
            <input
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setPage(0);
              }}
              placeholder="Search symbol"
              aria-label="Search symbol"
              className="w-full bg-transparent text-text-primary placeholder:text-text-muted focus:outline-none"
            />
          </label>
        </div>
      </div>
      <div className="overflow-x-auto px-1.5">
        <table className="w-full min-w-[820px] border-collapse text-left">
          <thead>
            <tr className="border-b border-border">
              <th className="px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-text-muted">Symbol</th>
              {TIMEFRAMES.map((tf) => (
                <th key={tf} className="px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-text-muted">{TIMEFRAME_SHORT[tf]}</th>
              ))}
              <th className="px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-text-muted">History from</th>
            </tr>
          </thead>
          <tbody>
            {data?.rows.map((row) => (
              <tr key={row.symbol_id} className="border-b border-border last:border-b-0">
                <td className="px-3 py-2.5 text-[13px] font-semibold text-text-primary">
                  {row.symbol}
                  {row.failed && <span className="ml-2 rounded bg-negative-soft px-1.5 py-0.5 text-[10.5px] font-medium text-negative">failed</span>}
                </td>
                {TIMEFRAMES.map((tf) => (
                  <td key={tf} className="px-3 py-2.5"><Cell cell={row.cells[tf]} /></td>
                ))}
                <td className="px-3 py-2.5 font-financial text-[13px] text-text-muted">{fmtLongDay(row.history_from)}</td>
              </tr>
            ))}
            {data && data.rows.length === 0 && (
              <tr>
                <td colSpan={TIMEFRAMES.length + 2} className="px-3 py-6 text-center text-[13px] text-text-muted">
                  {isLoading ? "Loading..." : "Nothing matches."}
                </td>
              </tr>
            )}
            {!data && (
              <tr>
                <td colSpan={TIMEFRAMES.length + 2} className="px-3 py-6 text-center text-[13px] text-text-muted">Loading...</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {data && data.total > PAGE_SIZE && (
        <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-2 text-xs text-text-muted">
          <span className="font-financial">
            {(first + 1).toLocaleString("en-IN")}-{Math.min(first + PAGE_SIZE, data.total).toLocaleString("en-IN")} of {data.total.toLocaleString("en-IN")}
          </span>
          <Button size="sm" variant="ghost" className="h-7 px-2" disabled={page === 0} onClick={() => setPage((p) => p - 1)} aria-label="Previous page">
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <Button size="sm" variant="ghost" className="h-7 px-2" disabled={first + PAGE_SIZE >= data.total} onClick={() => setPage((p) => p + 1)} aria-label="Next page">
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      )}
    </Card>
  );
}
