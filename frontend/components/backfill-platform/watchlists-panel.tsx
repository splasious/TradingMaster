"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, Plus, Trash2, Upload } from "lucide-react";
import Link from "next/link";
import { useRef, useState } from "react";

import { InstrumentMultiSelect } from "@/components/market-data/instrument-multiselect";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiDownload, apiFetch, ApiError, getAccessToken } from "@/lib/api";
import { useBfWatchlistItems, useBfWatchlists } from "@/lib/hooks";
import type { BfSource, BfWatchlistOut, InstrumentOut, WatchlistBulkAddResult } from "@/lib/types";

const DATA_SOURCE_TO_BF_SOURCE: Record<string, BfSource> = {
  yahoo_nse: "yahoo",
  delta_exchange: "delta",
};

function WatchlistDetail({ watchlist, onClose }: { watchlist: BfWatchlistOut; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data: items, isLoading } = useBfWatchlistItems(watchlist.id);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [picked, setPicked] = useState<InstrumentOut[]>([]);

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
    mutationFn: () => apiFetch(`/api/v1/backfill-platform/watchlists/${watchlist.id}/backfill?timeframe=1d`, { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bf-jobs"] });
      queryClient.invalidateQueries({ queryKey: ["bf-watchlist-items", watchlist.id] });
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
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="secondary" onClick={() => bulkBackfillMutation.mutate()} disabled={bulkBackfillMutation.isPending || !items?.length}>
            {bulkBackfillMutation.isPending ? "Starting..." : "Backfill All (1d)"}
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
        {importMutation.data && (
          <p className="text-xs text-text-muted">Imported: {importMutation.data.added} added, {importMutation.data.skipped} skipped.</p>
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
              <tr><Th>Source</Th><Th>Symbol</Th><Th>Name</Th><Th className="text-right">Bars</Th><Th>Last Job</Th><Th /></tr>
            </Thead>
            <Tbody>
              {items.map((item) => (
                <tr key={item.id}>
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
          <div className="flex gap-2">
            <Input placeholder="New watchlist name..." value={newName} onChange={(e) => setNewName(e.target.value)} className="max-w-xs" />
            <Button size="sm" onClick={() => createMutation.mutate()} disabled={!newName || createMutation.isPending}>
              <Plus className="h-3.5 w-3.5" /> Create
            </Button>
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
