"use client";

import { Plus, X } from "lucide-react";
import { useEffect, useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { useInstruments, useQuotes } from "@/lib/hooks";
import type { InstrumentOut } from "@/lib/types";
import { useMarketDataSocket } from "@/lib/ws";

const STORAGE_KEY = "tm:dashboard:watchlist";

function loadStoredWatchlist(): InstrumentOut[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as InstrumentOut[]) : [];
  } catch {
    return [];
  }
}

/** A live-quotes watchlist sidebar, Kite-style. No backend "user
 * watchlist" entity exists in this app today (the closest thing,
 * bf_watchlists, is the Data Backfill Platform's own catalog-management
 * tool, not a per-user trading watchlist) -- so the symbol list is a v1,
 * browser-local convenience persisted in localStorage, not synced across
 * devices or to the server. Live LTP/% change reuses exactly the same
 * useMarketDataSocket + useQuotes pairing app/(app)/markets/page.tsx
 * already uses for real live prices. */
export function WatchlistPanel({
  selectedId,
  onSelect,
}: {
  selectedId: string | null;
  onSelect: (instrument: InstrumentOut) => void;
}) {
  // Safe to read localStorage directly in the initializer rather than
  // syncing it in via an effect -- this panel only ever renders inside
  // the (app) layout's post-auth-check client tree (see sidebar.tsx's
  // identical reasoning), so there's no server-rendered markup here to
  // hydration-mismatch against.
  const [items, setItems] = useState<InstrumentOut[]>(loadStoredWatchlist);
  const [query, setQuery] = useState("");

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
  }, [items]);

  const { data: searchResults } = useInstruments(query, undefined, 10, query.length > 0);
  const instrumentIds = items.map((i) => i.id);
  const { prices } = useMarketDataSocket(instrumentIds);
  const { data: quotes } = useQuotes(instrumentIds);
  const quoteByInstrument = new Map((quotes ?? []).map((q) => [q.instrument_id, q]));

  function addInstrument(instrument: InstrumentOut) {
    setItems((prev) => (prev.some((i) => i.id === instrument.id) ? prev : [...prev, instrument]));
    setQuery("");
  }

  function removeInstrument(id: string) {
    setItems((prev) => prev.filter((i) => i.id !== id));
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Watchlist</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="relative">
          <Input placeholder="Add symbol..." value={query} onChange={(e) => setQuery(e.target.value)} />
          {query && searchResults && searchResults.length > 0 && (
            <div className="absolute z-10 mt-1 w-full rounded-md border border-border bg-surface-elevated shadow-lg">
              {searchResults.slice(0, 8).map((instrument) => (
                <button
                  key={instrument.id}
                  onClick={() => addInstrument(instrument)}
                  className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-surface"
                >
                  <span className="font-medium text-text-primary">{instrument.symbol}</span>
                  <Plus className="h-3.5 w-3.5 text-text-muted" />
                </button>
              ))}
            </div>
          )}
        </div>

        {!items.length ? (
          <EmptyState title="Watchlist is empty" description="Search above to add a symbol." />
        ) : (
          <div className="divide-y divide-border">
            {items.map((instrument) => {
              const tick = prices[instrument.id];
              const quote = quoteByInstrument.get(instrument.id);
              const change = tick && quote?.prev_close ? ((tick.price - quote.prev_close) / quote.prev_close) * 100 : null;
              return (
                <button
                  key={instrument.id}
                  onClick={() => onSelect(instrument)}
                  className={`group flex w-full items-center justify-between py-2 text-left ${
                    selectedId === instrument.id ? "bg-active-soft" : ""
                  }`}
                >
                  <span className="px-2">
                    <span className="block text-sm font-medium text-text-primary">{instrument.symbol}</span>
                    <span className="block text-xs text-text-muted">{instrument.exchange}</span>
                  </span>
                  <span className="flex items-center gap-2 px-2">
                    <span className="text-right">
                      <span className="font-financial block text-sm text-text-primary">
                        {tick ? tick.price.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "--"}
                      </span>
                      {change !== null && (
                        <span className={`font-financial block text-xs ${change >= 0 ? "text-positive" : "text-negative"}`}>
                          {change >= 0 ? "+" : ""}
                          {change.toFixed(2)}%
                        </span>
                      )}
                    </span>
                    <span
                      role="button"
                      tabIndex={-1}
                      onClick={(e) => {
                        e.stopPropagation();
                        removeInstrument(instrument.id);
                      }}
                      className="text-text-muted opacity-0 hover:text-negative group-hover:opacity-100"
                    >
                      <X className="h-3.5 w-3.5" />
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
