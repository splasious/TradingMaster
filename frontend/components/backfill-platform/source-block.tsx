"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Download, Loader2, Plus, XCircle } from "lucide-react";
import { useState } from "react";

import { CompletenessHeatmap } from "@/components/backfill-platform/completeness-heatmap";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { apiDownload, apiFetch, ApiError } from "@/lib/api";
import { useBfCompleteness, useBfJobs, useBfSourceStatus, useBfWatchlists } from "@/lib/hooks";
import type { BfBackfillJobOut, BfSource, SymbolSearchResultOut } from "@/lib/types";

const TIMEFRAMES = ["1m", "5m", "15m", "30m", "60m", "1d"];
const SOURCE_LABEL: Record<BfSource, string> = { yahoo: "Yahoo Finance", delta: "Delta Exchange", zerodha: "Zerodha Kite" };

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}
function daysAgoIso(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
}

export function SourceBlock({ source }: { source: BfSource }) {
  const queryClient = useQueryClient();
  const { data: status } = useBfSourceStatus(source);
  const { data: watchlists } = useBfWatchlists();

  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<SymbolSearchResultOut | null>(null);
  const [timeframe, setTimeframe] = useState("1d");
  const [startDate, setStartDate] = useState(daysAgoIso(90));
  const [endDate, setEndDate] = useState(todayIso());
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [watchlistToAdd, setWatchlistToAdd] = useState("");

  const { data: searchResults } = useQuery({
    queryKey: ["bf-symbol-search", source, query],
    queryFn: () => apiFetch<SymbolSearchResultOut[]>(`/api/v1/backfill-platform/sources/${source}/symbols?q=${encodeURIComponent(query)}`),
    enabled: query.length >= 2,
  });

  const { data: jobs } = useBfJobs(source);
  const activeJob = jobs?.find((j) => j.id === activeJobId) ?? null;

  const { data: completeness } = useBfCompleteness(source, selected?.symbol ?? null, timeframe, startDate, endDate);

  const backfillMutation = useMutation({
    mutationFn: () =>
      apiFetch<BfBackfillJobOut>("/api/v1/backfill-platform/jobs", {
        method: "POST",
        body: JSON.stringify({
          source, symbol: selected!.symbol, display_name: selected!.display_name, timeframe,
          start_date: startDate, end_date: endDate,
        }),
      }),
    onSuccess: (job) => {
      setActiveJobId(job.id);
      queryClient.invalidateQueries({ queryKey: ["bf-jobs"] });
      queryClient.invalidateQueries({ queryKey: ["bf-completeness"] });
    },
  });

  const addToWatchlistMutation = useMutation({
    mutationFn: () =>
      apiFetch(`/api/v1/backfill-platform/watchlists/${watchlistToAdd}/items`, {
        method: "POST",
        body: JSON.stringify({ source, symbol: selected!.symbol, display_name: selected!.display_name }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bf-watchlists"] });
      queryClient.invalidateQueries({ queryKey: ["bf-watchlist-items"] });
    },
  });

  return (
    <Card className="flex flex-col">
      <CardHeader className="flex-col items-start gap-1.5">
        <div className="flex w-full items-center justify-between">
          <CardTitle>{SOURCE_LABEL[source]}</CardTitle>
          {status && (
            <Badge tone={status.connected ? "positive" : "critical"}>
              {status.connected ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
              {status.connected ? "Connected" : "Disconnected"}
            </Badge>
          )}
        </div>
        {status && <p className="text-xs text-text-muted">{status.detail}</p>}
        {status?.expires_at && (
          <p className="text-xs text-warning">Session expires (est.) {new Date(status.expires_at).toLocaleString()}</p>
        )}
      </CardHeader>
      <CardContent className="flex-1 space-y-4">
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-text-secondary">Symbol</label>
          <Input placeholder="Search..." value={query} onChange={(e) => { setQuery(e.target.value); setSelected(null); }} />
          {query.length >= 2 && searchResults && !selected && (
            <div className="max-h-32 overflow-y-auto rounded-md border border-border">
              {searchResults.length === 0 ? (
                <p className="p-2 text-xs text-text-muted">No matches.</p>
              ) : (
                searchResults.map((r) => (
                  <button
                    key={r.symbol}
                    onClick={() => { setSelected(r); setQuery(""); }}
                    className="block w-full px-2 py-1.5 text-left text-sm text-text-secondary hover:bg-surface-elevated"
                  >
                    {r.symbol} <span className="text-text-muted">({r.display_name})</span>
                  </button>
                ))
              )}
            </div>
          )}
          {selected && <Badge tone="active">{selected.symbol}</Badge>}
        </div>

        {selected && (
          <>
            <div className="grid grid-cols-2 gap-2">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-text-secondary">Timeframe</label>
                <Select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
                  {TIMEFRAMES.map((tf) => <option key={tf} value={tf}>{tf}</option>)}
                </Select>
              </div>
              <div />
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-text-secondary">From</label>
                <Input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-text-secondary">To</label>
                <Input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
              </div>
            </div>

            <Button size="sm" onClick={() => backfillMutation.mutate()} disabled={backfillMutation.isPending || activeJob?.status === "running"}>
              {activeJob?.status === "running" ? "Backfilling..." : "Backfill"}
            </Button>

            {watchlists && watchlists.length > 0 && (
              <div className="flex items-center gap-1.5">
                <Select className="flex-1" value={watchlistToAdd} onChange={(e) => setWatchlistToAdd(e.target.value)}>
                  <option value="" disabled>Add to watchlist...</option>
                  {watchlists.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
                </Select>
                <Button size="sm" variant="secondary" onClick={() => addToWatchlistMutation.mutate()} disabled={!watchlistToAdd || addToWatchlistMutation.isPending}>
                  <Plus className="h-3.5 w-3.5" />
                </Button>
              </div>
            )}
            {addToWatchlistMutation.isSuccess && <p className="text-xs text-positive">Added to watchlist.</p>}

            {backfillMutation.isError && (
              <p className="text-xs text-negative">{backfillMutation.error instanceof ApiError ? backfillMutation.error.message : "Backfill failed to start"}</p>
            )}

            {activeJob && (
              <div className="flex items-center gap-2 text-xs">
                {activeJob.status === "running" || activeJob.status === "pending" ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-active" />
                ) : activeJob.status === "completed" ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-positive" />
                ) : (
                  <XCircle className="h-3.5 w-3.5 text-negative" />
                )}
                <span className="text-text-secondary">
                  {activeJob.status === "completed"
                    ? `Done -- ${activeJob.inserted_count} new, ${activeJob.duplicate_count} already had`
                    : activeJob.status === "failed"
                      ? activeJob.error_message
                      : "Running..."}
                </span>
              </div>
            )}

            {completeness && (
              <div>
                <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">Data Completeness</p>
                <CompletenessHeatmap segments={completeness.segments} rangeStart={startDate} rangeEnd={endDate} />
              </div>
            )}

            <Button
              variant="secondary" size="sm"
              onClick={() => apiDownload(`/api/v1/backfill-platform/export/symbol.xlsx?source=${source}&symbol=${selected.symbol}&timeframe=${timeframe}`, `${source}_${selected.symbol}.xlsx`)}
            >
              <Download className="h-3.5 w-3.5" /> Export Excel
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}
