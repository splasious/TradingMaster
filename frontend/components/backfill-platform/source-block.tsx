"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Download, Layers, Loader2, Plus, XCircle } from "lucide-react";
import { useState } from "react";

import { CompletenessHeatmap } from "@/components/backfill-platform/completeness-heatmap";
import { TimeframeMultiSelect } from "@/components/backfill-platform/timeframe-multiselect";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { apiDownload, apiFetch, ApiError } from "@/lib/api";
import { useBfCompleteness, useBfJobs, useBfSourceStatus, useBfTimeframes, useBfWatchlists } from "@/lib/hooks";
import type { BfBackfillJobOut, BfSource, BulkBackfillResult, SymbolSearchResultOut } from "@/lib/types";

const SOURCE_LABEL: Record<BfSource, string> = { yahoo: "Yahoo Finance", delta: "Delta Exchange", zerodha: "Zerodha Kite" };
const BULK_LABEL: Record<BfSource, string> = {
  yahoo: "Backfill All NSE Symbols",
  delta: "Backfill All RWA Tokens",
  zerodha: "Backfill All Tracked Symbols",
};

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}
function daysAgoIso(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
}

function JobStatusBanner({ job }: { job: BfBackfillJobOut }) {
  const isRunning = job.status === "running" || job.status === "pending";
  const isDone = job.status === "completed";
  const tone = isRunning ? "border-active bg-active-soft" : isDone ? "border-positive bg-positive-soft" : "border-negative bg-negative-soft";
  return (
    <div className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm ${tone}`}>
      {isRunning ? (
        <Loader2 className="h-4 w-4 shrink-0 animate-spin text-active" />
      ) : isDone ? (
        <CheckCircle2 className="h-4 w-4 shrink-0 text-positive" />
      ) : (
        <XCircle className="h-4 w-4 shrink-0 text-negative" />
      )}
      <span className={isRunning ? "text-active" : isDone ? "text-positive" : "text-negative"}>
        {isDone
          ? `Backfill complete -- ${job.inserted_count} new bar${job.inserted_count === 1 ? "" : "s"}, ${job.duplicate_count} already had`
          : isRunning
            ? `${job.status === "pending" ? "Queued" : "Backfilling"}: ${job.symbol} (${job.timeframe})...`
            : job.error_message}
      </span>
    </div>
  );
}

export function SourceBlock({ source }: { source: BfSource }) {
  const queryClient = useQueryClient();
  const { data: status } = useBfSourceStatus(source);
  const { data: watchlists } = useBfWatchlists();
  const { data: timeframeOptions } = useBfTimeframes(source);

  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<SymbolSearchResultOut | null>(null);
  const [timeframes, setTimeframes] = useState<string[]>(["1d"]);
  const [startDate, setStartDate] = useState(daysAgoIso(90));
  const [endDate, setEndDate] = useState(todayIso());
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [queuedCount, setQueuedCount] = useState<number | null>(null);
  const [watchlistToAdd, setWatchlistToAdd] = useState("");

  // Completeness/export show one timeframe's view at a time -- the first
  // checked one -- rather than redesigning those into multi-timeframe views.
  const primaryTimeframe = timeframes[0] ?? "1d";

  const { data: searchResults } = useQuery({
    queryKey: ["bf-symbol-search", source, query],
    queryFn: () => apiFetch<SymbolSearchResultOut[]>(`/api/v1/backfill-platform/sources/${source}/symbols?q=${encodeURIComponent(query)}`),
    enabled: query.length >= 2,
  });

  const { data: jobs } = useBfJobs(source);
  const activeJob = jobs?.find((j) => j.id === activeJobId) ?? null;

  const { data: completeness } = useBfCompleteness(source, selected?.symbol ?? null, primaryTimeframe, startDate, endDate);

  const backfillMutation = useMutation({
    mutationFn: async () => {
      const results = await Promise.allSettled(
        timeframes.map((tf) =>
          apiFetch<BfBackfillJobOut>("/api/v1/backfill-platform/jobs", {
            method: "POST",
            body: JSON.stringify({
              source, symbol: selected!.symbol, display_name: selected!.display_name, timeframe: tf,
              start_date: startDate, end_date: endDate,
            }),
          }),
        ),
      );
      const succeeded = results.filter((r): r is PromiseFulfilledResult<BfBackfillJobOut> => r.status === "fulfilled");
      const failed = results.filter((r): r is PromiseRejectedResult => r.status === "rejected");
      if (!succeeded.length && failed.length) {
        const first = failed[0].reason;
        throw first instanceof ApiError ? first : new Error("Backfill failed to start");
      }
      return succeeded.map((r) => r.value);
    },
    onSuccess: (jobs) => {
      if (jobs.length === 1) {
        setActiveJobId(jobs[0].id);
        setQueuedCount(null);
      } else {
        setActiveJobId(null);
        setQueuedCount(jobs.length);
      }
      queryClient.invalidateQueries({ queryKey: ["bf-jobs"] });
      queryClient.invalidateQueries({ queryKey: ["bf-completeness"] });
    },
  });

  const bulkBackfillMutation = useMutation({
    mutationFn: async () => {
      const results = await Promise.all(
        timeframes.map((tf) =>
          apiFetch<BulkBackfillResult>(`/api/v1/backfill-platform/sources/${source}/backfill-all?timeframe=${tf}`, { method: "POST" }),
        ),
      );
      return results.reduce((sum, r) => sum + r.queued, 0);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["bf-jobs"] }),
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

  const canBackfill = !!selected && (status?.connected ?? false);

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

        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">Timeframe(s)</label>
            <TimeframeMultiSelect options={timeframeOptions} value={timeframes} onChange={setTimeframes} />
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

        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            onClick={() => backfillMutation.mutate()}
            disabled={!canBackfill || backfillMutation.isPending || activeJob?.status === "running" || activeJob?.status === "pending"}
            title={!selected ? "Search and pick a symbol first" : !status?.connected ? "Source is disconnected" : undefined}
          >
            {backfillMutation.isPending || activeJob?.status === "running" || activeJob?.status === "pending"
              ? "Backfilling..."
              : timeframes.length > 1
                ? `Backfill (${timeframes.length} timeframes)`
                : "Backfill"}
          </Button>
          <Button
            size="sm" variant="secondary"
            onClick={() => bulkBackfillMutation.mutate()}
            disabled={!status?.connected || bulkBackfillMutation.isPending}
          >
            <Layers className="h-3.5 w-3.5" /> {bulkBackfillMutation.isPending ? "Queuing..." : BULK_LABEL[source]}
          </Button>
        </div>

        {bulkBackfillMutation.data != null && (
          <p className="text-xs text-positive">Queued {bulkBackfillMutation.data} background jobs -- see Job History below.</p>
        )}
        {bulkBackfillMutation.isError && (
          <p className="text-xs text-negative">
            {bulkBackfillMutation.error instanceof ApiError ? bulkBackfillMutation.error.message : "Bulk backfill failed to start"}
          </p>
        )}
        {queuedCount != null && (
          <p className="text-xs text-positive">Queued {queuedCount} backfill jobs ({timeframes.join(", ")}) -- see Job History below.</p>
        )}

        {watchlists && watchlists.length > 0 && selected && (
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

        {activeJob && <JobStatusBanner job={activeJob} />}

        {selected && completeness && (
          <div>
            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">Data Completeness</p>
            <CompletenessHeatmap segments={completeness.segments} rangeStart={startDate} rangeEnd={endDate} />
          </div>
        )}

        {selected && (
          <Button
            variant="secondary" size="sm"
            onClick={() => apiDownload(`/api/v1/backfill-platform/export/symbol.xlsx?source=${source}&symbol=${selected.symbol}&timeframe=${primaryTimeframe}`, `${source}_${selected.symbol}.xlsx`)}
          >
            <Download className="h-3.5 w-3.5" /> Export Excel
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
