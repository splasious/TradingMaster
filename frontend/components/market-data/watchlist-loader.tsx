"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { apiFetch } from "@/lib/api";
import { useBfWatchlists } from "@/lib/hooks";
import type { BfWatchlistItemOut, InstrumentOut } from "@/lib/types";

const BF_SOURCE_TO_EXCHANGE: Record<string, string> = { yahoo: "NSE", delta: "DELTA" };

interface WatchlistLoaderProps {
  onLoad: (instruments: InstrumentOut[]) => void;
}

/** Pulls a saved Data Backfill Platform watchlist's symbols into the main
 * instrument catalog -- resolving each bf_watchlist symbol to its matching
 * row here by (symbol, exchange), since the two schemas are deliberately
 * isolated and don't share ids. Reused across Strategy Builder, Backtesting,
 * and Optimization so a watchlist built once in Market Data Management can
 * be loaded anywhere instruments are selected. */
export function WatchlistLoader({ onLoad }: WatchlistLoaderProps) {
  const { data: watchlists } = useBfWatchlists();
  const [watchlistId, setWatchlistId] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ loaded: number; skipped: number } | null>(null);

  async function handleLoad() {
    if (!watchlistId) return;
    setLoading(true);
    setResult(null);
    try {
      const items = await apiFetch<BfWatchlistItemOut[]>(`/api/v1/backfill-platform/watchlists/${watchlistId}/items`);
      let skipped = 0;
      const resolved = (
        await Promise.all(
          items.map(async (item): Promise<InstrumentOut | null> => {
            const exchange = BF_SOURCE_TO_EXCHANGE[item.source];
            if (!exchange) {
              skipped += 1;
              return null;
            }
            const matches = await apiFetch<InstrumentOut[]>(
              `/api/v1/instruments?q=${encodeURIComponent(item.symbol)}&exchange=${exchange}`,
            );
            const exact = matches.find((m) => m.symbol === item.symbol) ?? null;
            if (!exact) skipped += 1;
            return exact;
          }),
        )
      ).filter((i): i is InstrumentOut => i !== null);

      onLoad(resolved);
      setResult({ loaded: resolved.length, skipped });
    } finally {
      setLoading(false);
    }
  }

  if (!watchlists?.length) return null;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select value={watchlistId} onChange={(e) => setWatchlistId(e.target.value)} className="w-52">
        <option value="">Load from watchlist...</option>
        {watchlists.map((w) => (
          <option key={w.id} value={w.id}>
            {w.name} ({w.symbol_count})
          </option>
        ))}
      </Select>
      <Button size="sm" variant="secondary" onClick={handleLoad} disabled={!watchlistId || loading}>
        {loading ? "Loading..." : "Load"}
      </Button>
      {result && (
        <span className="text-xs text-text-muted">
          {result.loaded} loaded{result.skipped ? `, ${result.skipped} skipped (not in instrument catalog)` : ""}
        </span>
      )}
    </div>
  );
}
