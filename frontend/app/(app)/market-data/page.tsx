"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, RefreshCw, XCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { JobHistoryPanel } from "@/components/backfill-platform/job-history";
import { SourceBlock } from "@/components/backfill-platform/source-block";
import { WatchlistsPanel } from "@/components/backfill-platform/watchlists-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, Tbody, Td } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useBackfillJob, useInstruments, useQuality } from "@/lib/hooks";
import { TIMEFRAMES, type BackfillJobOut, type InstrumentOut, type InstrumentSyncResult } from "@/lib/types";

function SyncSourceButton({ dataSource, label }: { dataSource: string; label: string }) {
  const queryClient = useQueryClient();
  const syncMutation = useMutation({
    mutationFn: () => apiFetch<InstrumentSyncResult>(`/api/v1/instruments/sync/${dataSource}`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["instruments"] }),
  });

  return (
    <div className="space-y-1.5">
      <Button variant="secondary" size="sm" onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
        <RefreshCw className={`h-3.5 w-3.5 ${syncMutation.isPending ? "animate-spin" : ""}`} />
        {syncMutation.isPending ? "Syncing..." : label}
      </Button>
      {syncMutation.data && (
        <p className="text-xs text-text-muted">
          Found {syncMutation.data.found}, added <span className="font-financial text-positive">{syncMutation.data.created}</span> new,
          skipped {syncMutation.data.skipped} (already tracked or inactive).
        </p>
      )}
      {syncMutation.isError && (
        <p className="text-xs text-negative">
          {syncMutation.error instanceof ApiError ? syncMutation.error.message : "Sync failed"}
        </p>
      )}
    </div>
  );
}

function SyncInstrumentCatalog() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Sync Instrument Catalog</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-text-muted">
          Pulls the full tracked universe from each real data source into the instrument catalog above -- the
          nse-yahoo-data service&apos;s ~750 NSE symbols, or Delta Exchange&apos;s live perpetual futures list. Safe to
          run repeatedly -- already-tracked instruments are skipped, never duplicated.
        </p>
        <div className="flex flex-wrap gap-6">
          <SyncSourceButton dataSource="yahoo_nse" label="Sync NSE Universe (Yahoo)" />
          <SyncSourceButton dataSource="delta_exchange" label="Sync Delta Markets" />
        </div>
      </CardContent>
    </Card>
  );
}

function InstrumentPicker({
  selected,
  onSelect,
}: {
  selected: InstrumentOut | null;
  onSelect: (i: InstrumentOut) => void;
}) {
  const [q, setQ] = useState("");
  const { data: instruments } = useInstruments(q);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Instruments</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <input
          className="h-9 w-full rounded-md border border-border bg-surface px-3 text-sm text-text-primary placeholder:text-text-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
          placeholder="Search..."
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <div className="max-h-96 space-y-0.5 overflow-y-auto">
          {instruments?.map((i) => (
            <button
              key={i.id}
              onClick={() => onSelect(i)}
              className={`flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm ${
                selected?.id === i.id ? "bg-active-soft text-active" : "text-text-secondary hover:bg-surface-elevated"
              }`}
            >
              <span>
                {i.symbol} <span className="text-text-muted">({i.exchange})</span>
              </span>
            </button>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function BackfillJobStatus({ job }: { job: BackfillJobOut }) {
  if (job.status === "completed") {
    return (
      <div className="flex items-center gap-2 text-sm text-positive">
        <CheckCircle2 className="h-4 w-4" />
        Completed -- downloaded {job.downloaded_count}, inserted {job.inserted_count}, duplicates {job.duplicate_count}
      </div>
    );
  }
  if (job.status === "failed") {
    return (
      <div className="flex items-start gap-2 text-sm text-negative">
        <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
        <span>{job.error_message}</span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 text-sm text-active">
      <Loader2 className="h-4 w-4 animate-spin" />
      {job.status === "pending" ? "Queued..." : "Running..."}
    </div>
  );
}

function QualityPanel({ instrumentId, timeframe }: { instrumentId: string; timeframe: string }) {
  const { data: quality, isLoading } = useQuality(instrumentId, timeframe);

  if (isLoading) return <p className="text-sm text-text-muted">Loading data quality...</p>;
  if (!quality || quality.candle_count === 0) return <p className="text-sm text-text-muted">No candles stored yet for this timeframe -- run a backfill.</p>;

  const tone = quality.quality_score >= 99 ? "positive" : quality.quality_score >= 90 ? "warning" : "critical";

  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-2">
        <span className="font-financial text-2xl font-semibold text-text-primary">{quality.quality_score}%</span>
        <Badge tone={tone}>Data Quality</Badge>
      </div>
      <Table>
        <Tbody>
          <tr>
            <Td className="text-text-secondary">Candles</Td>
            <Td className="text-right font-financial">{quality.candle_count.toLocaleString()}</Td>
          </tr>
          <tr>
            <Td className="text-text-secondary">Invalid OHLC relationships</Td>
            <Td className="text-right font-financial">{quality.invalid_ohlc_count}</Td>
          </tr>
          <tr>
            <Td className="text-text-secondary">Non-positive prices</Td>
            <Td className="text-right font-financial">{quality.non_positive_price_count}</Td>
          </tr>
          <tr>
            <Td className="text-text-secondary">Possible missing weekday candles</Td>
            <Td className="text-right font-financial">{quality.missing_weekday_gaps}</Td>
          </tr>
        </Tbody>
      </Table>
      {quality.missing_weekday_gaps > 0 && (
        <p className="text-xs text-text-muted">
          Heuristic gap count, not exchange-holiday-aware yet -- a later phase&apos;s market-session engine will
          replace this with real holiday calendars.
        </p>
      )}
    </div>
  );
}

export default function MarketDataPage() {
  const { hasRole } = useAuth();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<InstrumentOut | null>(null);
  const [timeframe, setTimeframe] = useState("1d");
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const canManage = hasRole("administrator", "trader", "analyst");

  const { data: job } = useBackfillJob(activeJobId);

  const backfillMutation = useMutation({
    mutationFn: () =>
      apiFetch<BackfillJobOut>("/api/v1/market-data/backfill", {
        method: "POST",
        body: JSON.stringify({ instrument_id: selected!.id, timeframe }),
      }),
    onSuccess: (data) => {
      setActiveJobId(data.id);
    },
  });

  // Once the job completes, the quality report is stale -- refetch it.
  useEffect(() => {
    if (job?.status === "completed" || job?.status === "failed") {
      queryClient.invalidateQueries({ queryKey: ["quality", selected?.id, timeframe] });
    }
  }, [job?.status, queryClient, selected?.id, timeframe]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Market Data Management</h1>
        <p className="text-sm text-text-muted">
          Backfill historical data, build watchlists, and check data completeness -- isolated per source.
        </p>
      </div>

      <Tabs defaultValue="backfill-platform">
        <TabsList>
          <TabsTrigger value="backfill-platform">Data Backfill Platform</TabsTrigger>
          <TabsTrigger value="instrument-catalog">Instrument Catalog (Strategies)</TabsTrigger>
        </TabsList>

        <TabsContent value="backfill-platform">
          <div className="space-y-6">
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
              <SourceBlock source="yahoo" />
              <SourceBlock source="delta" />
              <SourceBlock source="zerodha" />
            </div>
            <WatchlistsPanel />
            <JobHistoryPanel />
          </div>
        </TabsContent>

        <TabsContent value="instrument-catalog">
          <div className="space-y-6">
            <p className="text-sm text-text-muted">
              This is the shared instrument catalog strategies, backtesting, and paper/live trading actually read
              from -- separate from the Data Backfill Platform above, which tracks its own independent copy per
              source (deliberately not merged, per that module&apos;s own spec).
            </p>

            {hasRole("administrator") && <SyncInstrumentCatalog />}

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
              <InstrumentPicker
                selected={selected}
                onSelect={(i) => {
                  setSelected(i);
                  setActiveJobId(null);
                }}
              />

              <Card className="lg:col-span-2">
                <CardHeader>
                  <CardTitle>{selected ? `${selected.symbol} -- ${selected.name}` : "Select an instrument"}</CardTitle>
                </CardHeader>
                <CardContent className="space-y-5">
                  {selected ? (
                    <>
                      <div className="flex items-end gap-3">
                        <div className="space-y-1.5">
                          <label className="text-sm font-medium text-text-secondary">Timeframe</label>
                          <Select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="w-32">
                            {TIMEFRAMES.map((tf) => (
                              <option key={tf} value={tf}>
                                {tf}
                              </option>
                            ))}
                          </Select>
                        </div>
                        {canManage && (
                          <Button
                            onClick={() => backfillMutation.mutate()}
                            disabled={backfillMutation.isPending || job?.status === "pending" || job?.status === "running"}
                          >
                            Run Backfill
                          </Button>
                        )}
                      </div>

                      {backfillMutation.error && (
                        <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
                          {backfillMutation.error instanceof ApiError ? backfillMutation.error.message : "Backfill failed to start"}
                        </div>
                      )}

                      {job && <BackfillJobStatus job={job} />}

                      <div className="border-t border-border pt-4">
                        <QualityPanel instrumentId={selected.id} timeframe={timeframe} />
                      </div>
                    </>
                  ) : (
                    <p className="text-sm text-text-muted">Pick an instrument from the list to manage its historical data.</p>
                  )}
                </CardContent>
              </Card>
            </div>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
