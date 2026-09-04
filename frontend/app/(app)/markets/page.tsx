"use client";

import Link from "next/link";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { ConnectionStatusBadge } from "@/components/ui/status-badge";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { DELTA_CATEGORY_OPTIONS, useDeltaCategoryMap, useInstruments, useQuotes } from "@/lib/hooks";
import { getDeltaCategory, marketLabel } from "@/lib/market";
import type { InstrumentOut } from "@/lib/types";
import { useMarketDataSocket } from "@/lib/ws";

type SortKey = "market" | "symbol" | "name" | "type" | "price" | "change" | "volume" | "update";

function SortableTh({
  label,
  sortKey,
  active,
  direction,
  onSort,
  align = "left",
}: {
  label: string;
  sortKey: SortKey;
  active: boolean;
  direction: "asc" | "desc";
  onSort: (key: SortKey) => void;
  align?: "left" | "right";
}) {
  const Icon = active ? (direction === "asc" ? ArrowUp : ArrowDown) : ArrowUpDown;
  return (
    <Th className={align === "right" ? "text-right" : undefined}>
      <button
        onClick={() => onSort(sortKey)}
        className={`inline-flex items-center gap-1 hover:text-text-secondary ${active ? "text-text-secondary" : ""}`}
      >
        {align === "right" && <Icon className="h-3 w-3" />}
        {label}
        {align === "left" && <Icon className="h-3 w-3" />}
      </button>
    </Th>
  );
}

export default function MarketsPage() {
  const [q, setQ] = useState("");
  const [exchange, setExchange] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const { data: rawInstruments, isLoading, isError } = useInstruments(q, exchange || undefined);
  const categoryMap = useDeltaCategoryMap();
  // NSE/Yahoo is hidden app-wide -- no reachable data source in production.
  const instruments = useMemo(
    () =>
      rawInstruments?.filter(
        (i) => i.data_source !== "yahoo_nse" && (!categoryFilter || getDeltaCategory(i.symbol, categoryMap) === categoryFilter),
      ),
    [rawInstruments, categoryFilter, categoryMap],
  );

  const instrumentIds = useMemo(() => (instruments ?? []).map((i) => i.id), [instruments]);
  const { status, prices, latencyMs } = useMarketDataSocket(instrumentIds);
  const { data: quotes } = useQuotes(instrumentIds);
  const quoteByInstrument = useMemo(() => new Map((quotes ?? []).map((q) => [q.instrument_id, q] as const)), [quotes]);

  function pctChange(instrumentId: string): number | null {
    const tick = prices[instrumentId];
    const quote = quoteByInstrument.get(instrumentId);
    if (!tick || !quote || !quote.prev_close) return null;
    return ((tick.price - quote.prev_close) / quote.prev_close) * 100;
  }

  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  }

  const sortedInstruments = useMemo(() => {
    const list = [...(instruments ?? [])];
    if (!sortKey) return list;
    const key: SortKey = sortKey;

    function value(i: InstrumentOut): string | number | null {
      switch (key) {
        case "market":
          return marketLabel(i.exchange);
        case "symbol":
          return i.symbol;
        case "name":
          return i.name;
        case "type":
          return i.instrument_type;
        case "price":
          return prices[i.id]?.price ?? null;
        case "change":
          return pctChange(i.id);
        case "volume":
          return quoteByInstrument.get(i.id)?.volume ?? null;
        case "update":
          return prices[i.id]?.ts ?? null;
      }
    }

    const dir = sortDir === "asc" ? 1 : -1;
    return list.sort((a, b) => {
      const va = value(a);
      const vb = value(b);
      if (va === null && vb === null) return 0;
      if (va === null) return 1; // missing values always sort last, regardless of direction
      if (vb === null) return -1;
      if (typeof va === "string" || typeof vb === "string") return dir * String(va).localeCompare(String(vb));
      return dir * (va - vb);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- pctChange/value close over prices/quoteByInstrument, already deps below
  }, [instruments, sortKey, sortDir, prices, quoteByInstrument]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Markets</h1>
          <p className="text-sm text-text-muted">Delta Markets (real history, public API).</p>
        </div>
        <div className="flex items-center gap-3">
          {latencyMs !== null && <span className="font-financial text-xs text-text-muted">{latencyMs}ms</span>}
          <ConnectionStatusBadge status={status} />
        </div>
      </div>

      <div className="flex items-center gap-3">
        <Input placeholder="Search symbol or name..." value={q} onChange={(e) => setQ(e.target.value)} className="max-w-xs" />
        <Select value={exchange} onChange={(e) => setExchange(e.target.value)} className="w-40">
          <option value="">All Markets</option>
          <option value="DELTA">Delta Markets</option>
        </Select>
        <Select value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)} className="w-40">
          <option value="">All Categories</option>
          {DELTA_CATEGORY_OPTIONS.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </Select>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Instruments</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <LoadingState />
          ) : isError ? (
            <ErrorState description="Could not load the instrument catalog." />
          ) : !instruments?.length ? (
            <EmptyState title="No instruments match" description="Try a different search term or market filter." />
          ) : (
            <Table>
              <Thead>
                <tr>
                  <SortableTh label="Market" sortKey="market" active={sortKey === "market"} direction={sortDir} onSort={toggleSort} />
                  <SortableTh label="Symbol" sortKey="symbol" active={sortKey === "symbol"} direction={sortDir} onSort={toggleSort} />
                  <SortableTh label="Name" sortKey="name" active={sortKey === "name"} direction={sortDir} onSort={toggleSort} />
                  <SortableTh label="Type" sortKey="type" active={sortKey === "type"} direction={sortDir} onSort={toggleSort} />
                  <SortableTh label="Live Price" sortKey="price" active={sortKey === "price"} direction={sortDir} onSort={toggleSort} align="right" />
                  <SortableTh label="% Change" sortKey="change" active={sortKey === "change"} direction={sortDir} onSort={toggleSort} align="right" />
                  <SortableTh label="Volume" sortKey="volume" active={sortKey === "volume"} direction={sortDir} onSort={toggleSort} align="right" />
                  <SortableTh label="Last Update" sortKey="update" active={sortKey === "update"} direction={sortDir} onSort={toggleSort} align="right" />
                </tr>
              </Thead>
              <Tbody>
                {sortedInstruments.map((instrument) => {
                  const tick = prices[instrument.id];
                  const quote = quoteByInstrument.get(instrument.id);
                  const change = pctChange(instrument.id);
                  const category = instrument.exchange === "DELTA" ? getDeltaCategory(instrument.symbol, categoryMap) : null;
                  return (
                    <tr key={instrument.id}>
                      <Td>
                        <div className="flex items-center gap-1.5">
                          <Badge tone="neutral">{marketLabel(instrument.exchange)}</Badge>
                          {category && <Badge tone="active">{category}</Badge>}
                        </div>
                      </Td>
                      <Td className="font-medium">
                        <Link
                          href={`/charts?instrument_id=${instrument.id}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-active underline underline-offset-2 hover:opacity-80"
                        >
                          {instrument.symbol}
                        </Link>
                      </Td>
                      <Td className="text-text-secondary">{instrument.name}</Td>
                      <Td className="capitalize text-text-secondary">{instrument.instrument_type.replace("_", " ")}</Td>
                      <Td className="text-right">
                        {tick ? (
                          <span className="font-financial">
                            {tick.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                            <span
                              className={`ml-1.5 text-[10px] font-normal uppercase ${
                                tick.source === "simulated" ? "text-text-muted" : "text-positive"
                              }`}
                            >
                              {tick.source}
                            </span>
                          </span>
                        ) : (
                          <span className="text-text-muted">--</span>
                        )}
                      </Td>
                      <Td className="text-right">
                        {change !== null ? (
                          <span className={`font-financial ${change >= 0 ? "text-positive" : "text-negative"}`}>
                            {change >= 0 ? "+" : ""}
                            {change.toFixed(2)}%
                          </span>
                        ) : (
                          <span className="text-text-muted">--</span>
                        )}
                      </Td>
                      <Td className="text-right font-financial text-text-secondary">
                        {quote?.volume != null ? quote.volume.toLocaleString(undefined, { maximumFractionDigits: 0 }) : <span className="text-text-muted">--</span>}
                      </Td>
                      <Td className="text-right text-text-secondary">
                        {tick ? (
                          <span className="font-financial text-xs" title={new Date(tick.ts).toISOString()}>
                            {new Date(tick.ts).toLocaleDateString()} {new Date(tick.ts).toLocaleTimeString()}
                          </span>
                        ) : (
                          <span className="text-text-muted">--</span>
                        )}
                      </Td>
                    </tr>
                  );
                })}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
