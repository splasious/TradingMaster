"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { IChartApi } from "lightweight-charts";
import { useSearchParams } from "next/navigation";
import { X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

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
import { syncChartTimeScales } from "@/lib/chart-sync";
import { useChartCandles, useIndicator, useIndicatorList, useInstrument, useInstruments, useResampleBase } from "@/lib/hooks";
import { brokerForExchange, getDeltaCategory, marketLabel } from "@/lib/market";
import { TIMEFRAMES, type CatalogSyncItemOut, type IndicatorSpecOut, type InstrumentOut } from "@/lib/types";

const DATA_SOURCE_TO_BF_SOURCE: Record<string, string> = {
  yahoo_nse: "yahoo",
  delta_exchange: "delta",
};

const DEFAULT_ACTIVE_INDICATORS = ["sma", "rsi"];

const INDICATOR_COLORS = ["#3b6bf5", "#f59e0b", "#9333ea", "#16a34a", "#dc2626", "#0891b2", "#c026d3", "#65a30d", "#ea580c", "#4338ca"];

// Natural reference bands for the oscillators that have a conventional
// overbought/oversold pair -- purely a display aid, not used elsewhere.
const OSCILLATOR_BANDS: Record<string, number[]> = {
  rsi: [30, 70],
  stochastic: [20, 80],
  mfi: [20, 80],
  cci: [-100, 100],
  williams_r: [-80, -20],
  cmo: [-50, 50],
  ultimate_oscillator: [30, 70],
};

/** Renders nothing -- exists to call useIndicator once per active indicator
 * (a fixed hook call per mounted instance, keyed by code) and report the
 * resulting chart lines up to the parent, since the parent can't call a
 * variable-length list of hooks directly. */
function IndicatorSeries({
  instrumentId,
  timeframe,
  baseTimeframe,
  spec,
  color,
  onLines,
}: {
  instrumentId: string | null;
  timeframe: string;
  baseTimeframe: string | null;
  spec: IndicatorSpecOut;
  color: string;
  onLines: (code: string, lines: OverlayLine[] | null) => void;
}) {
  const { data } = useIndicator(instrumentId, timeframe, spec.code, baseTimeframe);
  useEffect(() => {
    if (!data) {
      onLines(spec.code, null);
      return;
    }
    const lines = spec.output_fields.map((field) => ({
      id: spec.output_fields.length > 1 ? `${spec.name} (${field})` : spec.name,
      color,
      points: data.map((p) => ({ ts: p.ts, value: p.values[field] })),
    }));
    onLines(spec.code, lines);
  }, [data, spec, color, onLines]);
  return null;
}

export default function ChartsPage() {
  const searchParams = useSearchParams();
  const deepLinkInstrumentId = searchParams.get("instrument_id");
  const deepLinkSymbol = searchParams.get("symbol");
  const deepLinkExchange = searchParams.get("exchange");
  const queryClient = useQueryClient();

  // The price chart and each oscillator panel below it are independent
  // lightweight-charts instances (own createChart(), own time scale) --
  // without this they don't move together when panning/zooming any one.
  const [priceChartApi, setPriceChartApi] = useState<IChartApi | null>(null);
  const [oscillatorChartApis, setOscillatorChartApis] = useState<Record<string, IChartApi | null>>({});
  useEffect(() => {
    const charts = [priceChartApi, ...Object.values(oscillatorChartApis)].filter((c): c is IChartApi => c !== null);
    if (charts.length < 2) return;
    return syncChartTimeScales(charts);
  }, [priceChartApi, oscillatorChartApis]);

  const [q, setQ] = useState("");
  const [exchange, setExchange] = useState("");
  const [selected, setSelected] = useState<InstrumentOut | null>(null);
  const [timeframe, setTimeframe] = useState("1d");
  const [activeIndicators, setActiveIndicators] = useState<string[]>(DEFAULT_ACTIVE_INDICATORS);
  const [indicatorLines, setIndicatorLines] = useState<Map<string, OverlayLine[]>>(new Map());

  const { data: indicatorList } = useIndicatorList();
  const specByCode = useMemo(() => new Map((indicatorList ?? []).map((s) => [s.code, s] as const)), [indicatorList]);

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

  const handleLines = useCallback((code: string, lines: OverlayLine[] | null) => {
    setIndicatorLines((prev) => {
      const next = new Map(prev);
      if (lines) next.set(code, lines);
      else next.delete(code);
      return next;
    });
  }, []);

  function addIndicator(code: string) {
    if (!code || activeIndicators.includes(code)) return;
    setActiveIndicators((prev) => [...prev, code]);
  }

  function removeIndicator(code: string) {
    setActiveIndicators((prev) => prev.filter((c) => c !== code));
    setIndicatorLines((prev) => {
      const next = new Map(prev);
      next.delete(code);
      return next;
    });
    setOscillatorChartApis((prev) => Object.fromEntries(Object.entries(prev).filter(([c]) => c !== code)));
  }

  const activeSpecs = activeIndicators.map((code) => specByCode.get(code)).filter((s): s is IndicatorSpecOut => !!s);
  const overlaySpecs = activeSpecs.filter((s) => s.overlay);
  const oscillatorSpecs = activeSpecs.filter((s) => !s.overlay);

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

  const overlays: OverlayLine[] = overlaySpecs.flatMap((s) => indicatorLines.get(s.code) ?? []);

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
      {resolvedSelected &&
        indicatorsAvailable &&
        activeSpecs.map((spec, i) => (
          <IndicatorSeries
            key={spec.code}
            instrumentId={resolvedSelected.id}
            timeframe={timeframe}
            baseTimeframe={effectiveBase}
            spec={spec}
            color={INDICATOR_COLORS[i % INDICATOR_COLORS.length]}
            onLines={handleLines}
          />
        ))}

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
            <div className="flex flex-1 flex-wrap items-center gap-3">
              <Select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="w-24">
                {TIMEFRAMES.map((tf) => (
                  <option key={tf} value={tf}>
                    {tf}
                  </option>
                ))}
              </Select>
              <Select
                value=""
                onChange={(e) => addIndicator(e.target.value)}
                disabled={!indicatorsAvailable}
                className="w-44"
              >
                <option value="">+ Add indicator...</option>
                {["trend", "momentum", "volatility", "volume", "structure"].map((category) => {
                  const options = (indicatorList ?? []).filter((s) => s.category === category && !activeIndicators.includes(s.code));
                  if (!options.length) return null;
                  return (
                    <optgroup key={category} label={category[0].toUpperCase() + category.slice(1)}>
                      {options.map((s) => (
                        <option key={s.code} value={s.code}>
                          {s.name}
                        </option>
                      ))}
                    </optgroup>
                  );
                })}
              </Select>
              {!indicatorsAvailable && <span className="text-xs text-text-muted">(no candles stored yet to compute indicators from)</span>}
            </div>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          {resolvedSelected && activeIndicators.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {activeIndicators.map((code, i) => (
                <Badge key={code} style={{ backgroundColor: `${INDICATOR_COLORS[i % INDICATOR_COLORS.length]}22`, color: INDICATOR_COLORS[i % INDICATOR_COLORS.length] }}>
                  {specByCode.get(code)?.name ?? code}
                  <button onClick={() => removeIndicator(code)}>
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              ))}
            </div>
          )}
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
              <PriceChart candles={candles} overlays={overlays} onChartReady={setPriceChartApi} />
              {oscillatorSpecs.map((spec) => (
                <div key={spec.code}>
                  <div className="mb-1 text-xs font-medium text-text-muted">{spec.name}</div>
                  <OscillatorChart
                    lines={indicatorLines.get(spec.code) ?? []}
                    bands={OSCILLATOR_BANDS[spec.code] ?? []}
                    onChartReady={(chart) => setOscillatorChartApis((prev) => ({ ...prev, [spec.code]: chart }))}
                  />
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
