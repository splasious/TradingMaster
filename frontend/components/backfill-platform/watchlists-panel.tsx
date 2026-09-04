"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, Plus, Trash2, Upload } from "lucide-react";
import Link from "next/link";
import { useRef, useState } from "react";

import { TimeframeMultiSelect } from "@/components/backfill-platform/timeframe-multiselect";
import { InstrumentMultiSelect } from "@/components/market-data/instrument-multiselect";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiDownload, apiFetch, ApiError, getAccessToken } from "@/lib/api";
import { useBfTimeframes, useBfWatchlistItems, useBfWatchlists } from "@/lib/hooks";
import type {
  BfBackfillJobOut,
  BfSource,
  BfWatchlistOut,
  InstrumentOut,
  TimeframeOptionOut,
  WatchlistBulkAddResult,
  WatchlistCatalogSyncResult,
} from "@/lib/types";

const DATA_SOURCE_TO_BF_SOURCE: Record<string, BfSource> = {
  yahoo_nse: "yahoo",
  delta_exchange: "delta",
};

/** Merges each source's own native timeframe set (they genuinely differ --
 * see timeframes.py) into one option list scoped to whichever sources this
 * particular watchlist's items actually use, so a mixed-source watchlist
 * doesn't offer a timeframe that would just fail for one of its items. */
function useUnionTimeframeOptions(presentSources: Set<BfSource>): TimeframeOptionOut[] {
  const { data: yahoo } = useBfTimeframes("yahoo");
  const { data: delta } = useBfTimeframes("delta");
  const { data: zerodha } = useBfTimeframes("zerodha");
  const bySource: Record<BfSource, TimeframeOptionOut[] | undefined> = { yahoo, delta, zerodha };
  const merged = new Map<string, TimeframeOptionOut>();
  for (const source of presentSources) {
    for (const opt of bySource[source] ?? []) {
      const existing = merged.get(opt.value);
      merged.set(opt.value, { value: opt.value, native: existing?.native || opt.native });
    }
  }
  const order = ["1m", "5m", "15m", "30m", "60m", "4h", "1d", "1wk", "1mo"];
  return [...merged.values()].sort((a, b) => order.indexOf(a.value) - order.indexOf(b.value));
}

function WatchlistDetail({ watchlist, onClose }: { watchlist: BfWatchlistOut; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data: items, isLoading } = useBfWatchlistItems(watchlist.id);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [picked, setPicked] = useState<InstrumentOut[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [timeframes, setTimeframes] = useState<string[]>(["1d"]);

  const presentSources = new Set((items ?? []).map((i) => i.source));
  const timeframeOptions = useUnionTimeframeOptions(presentSources);

  function toggleItem(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const allSelected = !!items?.length && items.every((i) => selectedIds.has(i.id));
  function toggleSelectAll() {
    setSelectedIds(allSelected ? new Set() : new Set((items ?? []).map((i) => i.id)));
  }

  const bulkAddMutation = useMutation({
    mutationFn: () =>
      apiFetch<WatchlistBulkAddResult>(`/api/v1/backfill-platform/watchlists/${watchlist.id}/items/bulk`, {
        method: "POST",
        body: JSON.stringify({
          items: picked
            .map((i) => ({ source: DATA_SOURCE_TO_BF_SOURCE[i.data_source], symbol: i.symbol, display_name: i.name }))
            .filter((i) => !!i.source),
        }),
      }),
    onSuccess: () => {
      setPicked([]);
      queryClient.invalidateQueries({ queryKey: ["bf-watchlist-items", watchlist.id] });
      queryClient.invalidateQueries({ queryKey: ["bf-watchlists"] });
    },
  });

  const removeItemMutation = useMutation({
    mutationFn: (itemId: string) => apiFetch(`/api/v1/backfill-platform/watchlists/${watchlist.id}/items/${itemId}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bf-watchlist-items", watchlist.id] });
      queryClient.invalidateQueries({ queryKey: ["bf-watchlists"] });
    },
  });

  const bulkBackfillMutation = useMutation({
    mutationFn: async () => {
      const itemIds = selectedIds.size ? [...selectedIds] : undefined;
      const results = await Promise.all(
        timeframes.map((tf) =>
          apiFetch<BfBackfillJobOut[]>(`/api/v1/backfill-platform/watchlists/${watchlist.id}/backfill?timeframe=${tf}`, {
            method: "POST",
            body: JSON.stringify({ item_ids: itemIds ?? null }),
          }),
        ),
      );
      return results.reduce((sum, jobs) => sum + jobs.length, 0);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bf-jobs"] });
      queryClient.invalidateQueries({ queryKey: ["bf-watchlist-items", watchlist.id] });
    },
  });

  const syncToCatalogMutation = useMutation({
    mutationFn: () =>
      apiFetch<WatchlistCatalogSyncResult>(`/api/v1/backfill-platform/watchlists/${watchlist.id}/sync-to-catalog`, { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["instruments"] });
    },
  });

  const importMutation = useMutation({
    mutationFn: async (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      const token = getAccessToken();
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/api/v1/backfill-platform/watchlists/${watchlist.id}/import`, {
        method: "POST", body: formData, credentials: "include",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new ApiError(res.status, "Import failed");
      return res.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bf-watchlist-items", watchlist.id] });
      queryClient.invalidateQueries({ queryKey: ["bf-watchlists"] });
    },
  });

  return (
    <Card>
      <CardHeader className="flex-col items-start gap-1">
        <div className="flex w-full items-center justify-between">
          <CardTitle>{watchlist.name}</CardTitle>
          <Button variant="ghost" size="sm" onClick={onClose}>Close</Button>
        </div>
        <div className="flex flex-wrap gap-1">
          {watchlist.tags.map((t) => <Badge key={t} tone="neutral">{t}</Badge>)}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-end gap-2">
          <div className="w-48 space-y-1">
            <label className="text-xs font-medium text-text-secondary">Timeframe(s)</label>
            <TimeframeMultiSelect options={timeframeOptions} value={timeframes} onChange={setTimeframes} />
          </div>
          <Button size="sm" onClick={() => bulkBackfillMutation.mutate()} disabled={bulkBackfillMutation.isPending || !items?.length}>
            {bulkBackfillMutation.isPending
              ? "Starting..."
              : selectedIds.size
                ? `Backfill Selected (${selectedIds.size})`
                : `Backfill All (${items?.length ?? 0})`}
          </Button>
          <Button
            size="sm" variant="secondary"
            onClick={() => syncToCatalogMutation.mutate()}
            disabled={syncToCatalogMutation.isPending || !items?.length}
            title="Copies this watchlist's backfilled bars into the main catalog so they show up in Charts, Strategy Builder, Backtesting, and Optimization"
          >
            {syncToCatalogMutation.isPending ? "Syncing..." : "Sync to Charts / Strategies"}
          </Button>
          <Button size="sm" variant="secondary" onClick={() => apiDownload(`/api/v1/backfill-platform/watchlists/${watchlist.id}/export.csv`, `${watchlist.name}.csv`)}>
            <Download className="h-3.5 w-3.5" /> Export CSV
          </Button>
          <Button size="sm" variant="secondary" onClick={() => apiDownload(`/api/v1/backfill-platform/watchlists/${watchlist.id}/export.xlsx`, `${watchlist.name}.xlsx`)}>
            <Download className="h-3.5 w-3.5" /> Export Excel
          </Button>
          <input ref={fileInputRef} type="file" accept=".csv" className="hidden" onChange={(e) => e.target.files?.[0] && importMutation.mutate(e.target.files[0])} />
          <Button size="sm" variant="secondary" onClick={() => fileInputRef.current?.click()} disabled={importMutation.isPending}>
            <Upload className="h-3.5 w-3.5" /> {importMutation.isPending ? "Importing..." : "Import CSV"}
          </Button>
        </div>
        <p className="text-xs text-text-muted">
          {selectedIds.size ? `${selectedIds.size} symbol(s) checked below -- backfill runs on just those.` : "Nothing checked -- backfill runs on every symbol in this watchlist."}
        </p>
        {bulkBackfillMutation.data != null && (
          <p className="text-xs text-positive">Queued {bulkBackfillMutation.data} background job(s) -- see Job History below.</p>
        )}
        {bulkBackfillMutation.isError && (
          <p className="text-xs text-negative">
            {bulkBackfillMutation.error instanceof ApiError ? bulkBackfillMutation.error.message : "Backfill failed to start"}
          </p>
        )}
        {importMutation.data && (
          <p className="text-xs text-text-muted">Imported: {importMutation.data.added} added, {importMutation.data.skipped} skipped.</p>
        )}
        {syncToCatalogMutation.data && (
          <p className="text-xs text-positive">
            Synced {syncToCatalogMutation.data.items.reduce((n, i) => n + i.bars_synced, 0)} bars across{" "}
            {syncToCatalogMutation.data.items.filter((i) => i.bars_synced > 0 || i.instrument_created).length} symbol(s) into the main catalog
            {syncToCatalogMutation.data.items.some((i) => i.error) && (
              <span className="text-negative">
                {" "}-- {syncToCatalogMutation.data.items.filter((i) => i.error).length} skipped (no catalog mapping for that source)
              </span>
            )}
            .
          </p>
        )}

        <div>
          <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-text-muted">
            Add symbols -- filter by exchange, tick to select
          </p>
          <InstrumentMultiSelect value={picked} onChange={setPicked} />
          <div className="mt-2 flex items-center gap-2">
            <Button size="sm" onClick={() => bulkAddMutation.mutate()} disabled={!picked.length || bulkAddMutation.isPending}>
              <Plus className="h-3.5 w-3.5" /> {bulkAddMutation.isPending ? "Adding..." : `Add ${picked.length || ""} to Watchlist`}
            </Button>
            {bulkAddMutation.data && (
              <span className="text-xs text-positive">
                Added {bulkAddMutation.data.added}, skipped {bulkAddMutation.data.skipped} already present.
              </span>
            )}
            {bulkAddMutation.isError && (
              <span className="text-xs text-negative">
                {bulkAddMutation.error instanceof ApiError ? bulkAddMutation.error.message : "Bulk add failed"}
              </span>
            )}
          </div>
        </div>

        {isLoading ? (
          <LoadingState />
        ) : !items?.length ? (
          <EmptyState title="No symbols yet" description="Add symbols from a source block above, or import a CSV." />
        ) : (
          <Table>
            <Thead>
              <tr>
                <Th><input type="checkbox" checked={allSelected} onChange={toggleSelectAll} aria-label="Select all" /></Th>
                <Th>Source</Th><Th>Symbol</Th><Th>Name</Th><Th className="text-right">Bars</Th><Th>Last Job</Th><Th />
              </tr>
            </Thead>
            <Tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <Td><input type="checkbox" checked={selectedIds.has(item.id)} onChange={() => toggleItem(item.id)} aria-label={`Select ${item.symbol}`} /></Td>
                  <Td className="capitalize">{item.source}</Td>
                  <Td className="font-medium">
                    {item.source === "yahoo" || item.source === "delta" ? (
                      <Link
                        href={`/charts?symbol=${encodeURIComponent(item.symbol)}&exchange=${item.source === "yahoo" ? "NSE" : "DELTA"}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-active underline underline-offset-2 hover:opacity-80"
                      >
                        {item.symbol}
                      </Link>
                    ) : (
                      item.symbol
                    )}
                  </Td>
                  <Td className="text-text-secondary">{item.display_name}</Td>
                  <Td className="text-right font-financial">{item.bar_count}</Td>
                  <Td>
                    {item.last_job_status ? <Badge tone={item.last_job_status === "completed" ? "positive" : item.last_job_status === "failed" ? "critical" : "active"}>{item.last_job_status}</Badge> : <span className="text-text-muted">--</span>}
                  </Td>
                  <Td className="text-right">
                    <Button variant="ghost" size="sm" onClick={() => removeItemMutation.mutate(item.id)}>
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </Td>
                </tr>
              ))}
            </Tbody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

export function WatchlistsPanel() {
  const queryClient = useQueryClient();
  const { data: watchlists, isLoading } = useBfWatchlists();
  const [newName, setNewName] = useState("");
  const [selected, setSelected] = useState<BfWatchlistOut | null>(null);

  const createMutation = useMutation({
    mutationFn: () => apiFetch<BfWatchlistOut>("/api/v1/backfill-platform/watchlists", { method: "POST", body: JSON.stringify({ name: newName, tags: [] }) }),
    onSuccess: () => {
      setNewName("");
      queryClient.invalidateQueries({ queryKey: ["bf-watchlists"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/backfill-platform/watchlists/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setSelected(null);
      queryClient.invalidateQueries({ queryKey: ["bf-watchlists"] });
    },
  });

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader><CardTitle>Watchlists</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2">
            <Input
              placeholder="New watchlist name..."
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && newName && createMutation.mutate()}
              className="max-w-xs"
            />
            <Button
              size="sm"
              onClick={() => createMutation.mutate()}
              disabled={!newName || createMutation.isPending}
              title={!newName ? "Type a name first" : undefined}
            >
              <Plus className="h-3.5 w-3.5" /> {createMutation.isPending ? "Creating..." : "Create"}
            </Button>
            {!newName && <span className="text-xs text-text-muted">Type a name to enable Create</span>}
            {createMutation.isError && (
              <span className="text-xs text-negative">
                {createMutation.error instanceof ApiError ? createMutation.error.message : "Failed to create watchlist"}
              </span>
            )}
          </div>

          {isLoading ? (
            <LoadingState />
          ) : !watchlists?.length ? (
            <EmptyState title="No watchlists yet" />
          ) : (
            <Table>
              <Thead>
                <tr><Th>Name</Th><Th className="text-right">Symbols</Th><Th className="text-right">Never Backfilled</Th><Th>Last Backfill</Th><Th /></tr>
              </Thead>
              <Tbody>
                {watchlists.map((w) => (
                  <tr key={w.id} className="cursor-pointer hover:bg-surface-elevated" onClick={() => setSelected(w)}>
                    <Td className="font-medium">{w.name}</Td>
                    <Td className="text-right font-financial">{w.symbol_count}</Td>
                    <Td className="text-right font-financial">{w.never_backfilled_count}</Td>
                    <Td className="text-text-muted">{w.last_backfill_at ? new Date(w.last_backfill_at).toLocaleString() : "Never"}</Td>
                    <Td className="text-right" onClick={(e) => e.stopPropagation()}>
                      <Button variant="ghost" size="sm" onClick={() => deleteMutation.mutate(w.id)}>
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </Td>
                  </tr>
                ))}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>

      {selected && <WatchlistDetail watchlist={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
