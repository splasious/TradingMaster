"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";

import { OscillatorChart } from "@/components/charts/oscillator-chart";
import { PriceChart, type OverlayLine } from "@/components/charts/price-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { MarketContextBar } from "@/components/trading/market-context-bar";
import { apiFetch, ApiError } from "@/lib/api";
import { useChartCandles, useIndicator, useInstrument, useInstruments, useResampleBase } from "@/lib/hooks";
import { brokerForExchange, getDeltaCategory, marketLabel } from "@/lib/market";
import { TIMEFRAMES, type CatalogSyncItemOut, type InstrumentOut } from "@/lib/types";

const DATA_SOURCE_TO_BF_SOURCE: Record<string, string> = {
  yahoo_nse: "yahoo",
  delta_exchange: "delta",
};

const OVERLAY_OPTIONS = [
  { code: "sma", field: "sma", label: "SMA 20", color: "#3b6bf5" },
  { code: "ema", field: "ema", label: "EMA 50", color: "#f59e0b" },
] as const;

function useOverlay(
  instrumentId: string | null,
  timeframe: string,
  baseTimeframe: string | null,
  code: string,
  field: string,
  color: string,
  enabled: boolean,
): OverlayLine | null {
  const { data } = useIndicator(enabled ? instrumentId : null, timeframe, code, baseTimeframe);
  return useMemo(() => {
    if (!enabled || !data) return null;
    return { id: code, color, points: data.map((p) => ({ ts: p.ts, value: p.values[field] })) };
  }, [enabled, data, code, field, color]);
}

export default function ChartsPage() {
  const searchParams = useSearchParams();
  const deepLinkInstrumentId = searchParams.get("instrument_id");
  const deepLinkSymbol = searchParams.get("symbol");
  const deepLinkExchange = searchParams.get("exchange");
  const queryClient = useQueryClient();

  const [q, setQ] = useState("");
  const [exchange, setExchange] = useState("");
  const [selected, setSelected] = useState<InstrumentOut | null>(null);
  const [timeframe, setTimeframe] = useState("1d");
  const [showSma, setShowSma] = useState(true);
  const [showEma, setShowEma] = useState(false);
  const [showBollinger, setShowBollinger] = useState(false);
  const [showRsi, setShowRsi] = useState(true);

  const { data: instruments } = useInstruments(q, exchange || undefined);

  // Deep-link support (e.g. hyperlinked symbols elsewhere in the app):
  // prefer ?instrument_id=, fall back to ?symbol=&exchange= for callers
  // that only have a symbol string (like the Data Backfill Platform's
  // isolated watchlists, which don't share ids with this main catalog).
  const { data: deepLinkById } = useInstrument(deepLinkInstrumentId);
  const { data: deepLinkBySymbolResults } = useInstruments(
    deepLinkSymbol ?? "",
    deepLinkExchange ?? undefined,
    undefined,
    !deepLinkInstrumentId && !!deepLinkSymbol,
  );

  // No explicit sync-into-state effect: the deep link is just a fallback
  // source for "which instrument is selected" -- an explicit click always
  // wins once it happens, so this is a plain derived value, not state.
  const resolvedSelected = useMemo<InstrumentOut | null>(() => {
    if (selected) return selected;
    if (deepLinkById) return deepLinkById;
    if (deepLinkSymbol && deepLinkBySymbolResults) {
      return deepLinkBySymbolResults.find((i) => i.symbol === deepLinkSymbol) ?? null;
    }
    return null;
  }, [selected, deepLinkById, deepLinkSymbol, deepLinkBySymbolResults]);

  const { data: candles, isLoading: candlesLoading, isError: candlesError } = useChartCandles(resolvedSelected?.id ?? null, timeframe);

  // Indicators derive from the same base timeframe the chart itself does
  // (real data directly, or resampled up from the finest real data on
  // file) -- available at whatever timeframe is selected, not just "1d".
  const { directlyAvailable, baseTimeframe: indicatorBase } = useResampleBase(resolvedSelected?.id ?? null, timeframe);
  const indicatorsAvailable = directlyAvailable || !!indicatorBase;
  const effectiveBase = directlyAvailable ? null : indicatorBase;

  const { data: rsi } = useIndicator(indicatorsAvailable && showRsi ? (resolvedSelected?.id ?? null) : null, timeframe, "rsi", effectiveBase);
  const { data: bollinger } = useIndicator(
    indicatorsAvailable && showBollinger ? (resolvedSelected?.id ?? null) : null,
    timeframe,
    "bollinger_bands",
    effectiveBase,
  );

  const smaLine = useOverlay(resolvedSelected?.id ?? null, timeframe, effectiveBase, "sma", "sma", "#3b6bf5", indicatorsAvailable && showSma);
  const emaLine = useOverlay(resolvedSelected?.id ?? null, timeframe, effectiveBase, "ema", "ema", "#f59e0b", indicatorsAvailable && showEma);

  const bfSource = resolvedSelected ? DATA_SOURCE_TO_BF_SOURCE[resolvedSelected.data_source] : undefined;
  const syncMutation = useMutation({
    mutationFn: () =>
      apiFetch<CatalogSyncItemOut>(
        `/api/v1/backfill-platform/sources/${bfSource}/symbols/${encodeURIComponent(resolvedSelected!.symbol)}/sync-to-catalog`,
        { method: "POST" },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chart-candles", resolvedSelected?.id] });
    },
  });

  const overlays = useMemo<OverlayLine[]>(() => {
    const lines: OverlayLine[] = [];
    if (smaLine) lines.push(smaLine);
    if (emaLine) lines.push(emaLine);
    if (showBollinger && bollinger) {
      lines.push({ id: "bb-upper", color: "#9333ea", points: bollinger.map((p) => ({ ts: p.ts, value: p.values.upper })) });
      lines.push({ id: "bb-lower", color: "#9333ea", points: bollinger.map((p) => ({ ts: p.ts, value: p.values.lower })) });
    }
    return lines;
  }, [smaLine, emaLine, showBollinger, bollinger]);

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
      <Card className="lg:col-span-1">
        <CardHeader>
          <CardTitle>Instruments</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <Input placeholder="Search..." value={q} onChange={(e) => setQ(e.target.value)} />
          <Select value={exchange} onChange={(e) => setExchange(e.target.value)}>
            <option value="">All Markets</option>
            <option value="NSE">NSE Markets</option>
            <option value="DELTA">Delta Markets</option>
          </Select>
          <div className="max-h-[32rem] space-y-0.5 overflow-y-auto">
            {instruments?.map((i) => {
              const category = i.exchange === "DELTA" ? getDeltaCategory(i.symbol) : null;
              return (
                <button
                  key={i.id}
                  onClick={() => setSelected(i)}
                  className={`flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm ${
                    resolvedSelected?.id === i.id ? "bg-active-soft text-active" : "text-text-secondary hover:bg-surface-elevated"
                  }`}
                >
                  <span>{i.symbol}</span>
                  <span className="flex items-center gap-1">
                    {category && <Badge tone="active">{category}</Badge>}
                    <span className="text-text-muted">{marketLabel(i.exchange)}</span>
                  </span>
                </button>
              );
            })}
          </div>
        </CardContent>
      </Card>

      <Card className="lg:col-span-3">
        <CardHeader className="flex-wrap gap-3">
          <CardTitle>{resolvedSelected ? `${resolvedSelected.symbol} -- ${resolvedSelected.name}` : "Select an instrument"}</CardTitle>
          {resolvedSelected && (
            <div className="flex flex-wrap items-center gap-3">
              <Select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="w-24">
                {TIMEFRAMES.map((tf) => (
                  <option key={tf} value={tf}>
                    {tf}
                  </option>
                ))}
              </Select>
              {OVERLAY_OPTIONS.map((opt) => (
                <label
                  key={opt.code}
                  className={`flex items-center gap-1.5 text-xs ${indicatorsAvailable ? "text-text-secondary" : "text-text-muted"}`}
                >
                  <input
                    type="checkbox"
                    disabled={!indicatorsAvailable}
                    checked={opt.code === "sma" ? showSma : showEma}
                    onChange={(e) => (opt.code === "sma" ? setShowSma(e.target.checked) : setShowEma(e.target.checked))}
                  />
                  {opt.label}
                </label>
              ))}
              <label className={`flex items-center gap-1.5 text-xs ${indicatorsAvailable ? "text-text-secondary" : "text-text-muted"}`}>
                <input type="checkbox" disabled={!indicatorsAvailable} checked={showBollinger} onChange={(e) => setShowBollinger(e.target.checked)} />
                Bollinger Bands
              </label>
              <label className={`flex items-center gap-1.5 text-xs ${indicatorsAvailable ? "text-text-secondary" : "text-text-muted"}`}>
                <input type="checkbox" disabled={!indicatorsAvailable} checked={showRsi} onChange={(e) => setShowRsi(e.target.checked)} />
                RSI
              </label>
              {!indicatorsAvailable && <span className="text-xs text-text-muted">(no candles stored yet to compute indicators from)</span>}
            </div>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          {resolvedSelected && (
            <MarketContextBar
              broker={brokerForExchange(resolvedSelected.exchange)}
              market={marketLabel(resolvedSelected.exchange)}
              instrument={resolvedSelected.symbol}
              instrumentType={resolvedSelected.instrument_type.replace("_", " ")}
              timeframe={timeframe}
              dataStatus={candlesError ? "disconnected" : candles?.length ? "live" : undefined}
            />
          )}
          {!resolvedSelected ? (
            <EmptyState title="No instrument selected" description="Pick an instrument to view its chart." />
          ) : candlesLoading ? (
            <LoadingState title="Loading candles..." />
          ) : candlesError ? (
            <ErrorState description="Could not load candle data for this instrument." />
          ) : !candles?.length ? (
            <EmptyState
              title="No candles stored"
              description={
                syncMutation.isError
                  ? syncMutation.error instanceof ApiError
                    ? syncMutation.error.message
                    : "Sync failed"
                  : bfSource
                    ? "Not yet in the main catalog. If it's been backfilled via the Data Backfill Platform, sync it in directly."
                    : "No candles stored for this timeframe yet -- run a backfill from Market Data first."
              }
              action={
                bfSource ? (
                  <Button size="sm" onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
                    {syncMutation.isPending ? "Syncing..." : "Sync from Backfill Platform"}
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <div className="space-y-2">
              <PriceChart candles={candles} overlays={overlays} />
              {showRsi && rsi && (
                <div>
                  <div className="mb-1 text-xs font-medium text-text-muted">RSI (14)</div>
                  <OscillatorChart points={rsi.map((p) => ({ ts: p.ts, value: p.values.rsi }))} bands={[30, 70]} />
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
