"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { IChartApi } from "lightweight-charts";
import { useSearchParams } from "next/navigation";
import { Plus, Settings2, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { OscillatorChart } from "@/components/charts/oscillator-chart";
import { PriceChart, type OverlayLine } from "@/components/charts/price-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
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

interface ActiveIndicator {
  code: string;
  color: string;
  bands: number[];
  params: Record<string, number>;
}

const DEFAULT_ACTIVE_CODES = ["sma", "rsi"];

// Stable fallback references -- a fresh `[]` literal on every render would
// give OscillatorChart/PriceChart a "changed" prop even when nothing
// actually changed, which used to cause unnecessary re-renders. Kept even
// after OscillatorChart itself stopped remounting on that (see its own
// comment) since it's free and avoids the churn entirely rather than just
// tolerating it.
const EMPTY_LINES: OverlayLine[] = [];
const EMPTY_BANDS: number[] = [];

const INDICATOR_COLORS = ["#3b6bf5", "#f59e0b", "#9333ea", "#16a34a", "#dc2626", "#0891b2", "#c026d3", "#65a30d", "#ea580c", "#4338ca"];

// Starting reference bands for the oscillators that have a conventional
// overbought/oversold pair -- just the initial value when an indicator is
// added; each active indicator keeps its own editable copy from there.
const DEFAULT_BANDS: Record<string, number[]> = {
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
  indicator,
  onLines,
}: {
  instrumentId: string | null;
  timeframe: string;
  baseTimeframe: string | null;
  spec: IndicatorSpecOut;
  indicator: ActiveIndicator;
  onLines: (code: string, lines: OverlayLine[] | null) => void;
}) {
  const { data } = useIndicator(instrumentId, timeframe, spec.code, baseTimeframe, indicator.params);
  useEffect(() => {
    if (!data) {
      onLines(spec.code, null);
      return;
    }
    const lines = spec.output_fields.map((field) => ({
      id: spec.output_fields.length > 1 ? `${spec.name} (${field})` : spec.name,
      color: indicator.color,
      points: data.map((p) => ({ ts: p.ts, value: p.values[field] })),
    }));
    onLines(spec.code, lines);
  }, [data, spec, indicator.color, onLines]);
  return null;
}

function IndicatorSettingsModal({
  spec,
  indicator,
  onChange,
  onClose,
}: {
  spec: IndicatorSpecOut;
  indicator: ActiveIndicator;
  onChange: (patch: Partial<ActiveIndicator>) => void;
  onClose: () => void;
}) {
  return (
    <Modal open onClose={onClose} title={`${spec.name} settings`}>
      <div className="space-y-4">
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-text-secondary">Line color</label>
          <div className="flex items-center gap-2">
            <input
              type="color"
              value={indicator.color}
              onChange={(e) => onChange({ color: e.target.value })}
              className="h-8 w-12 cursor-pointer rounded border border-border bg-transparent"
            />
            <span className="font-financial text-xs text-text-muted">{indicator.color}</span>
          </div>
        </div>

        {Object.keys(spec.default_params).length > 0 && (
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">Parameters</label>
            <div className="grid grid-cols-2 gap-2">
              {Object.keys(spec.default_params).map((key) => (
                <div key={key} className="space-y-1">
                  <label className="text-[11px] capitalize text-text-muted">{key.replace(/_/g, " ")}</label>
                  <Input
                    type="number"
                    value={indicator.params[key] ?? spec.default_params[key]}
                    onChange={(e) => onChange({ params: { ...indicator.params, [key]: Number(e.target.value) } })}
                  />
                </div>
              ))}
            </div>
          </div>
        )}

        {!spec.overlay && (
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">Reference bands</label>
            <p className="text-[11px] text-text-muted">Horizontal dashed lines drawn on this indicator&apos;s panel (e.g. RSI&apos;s 30/70 overbought-oversold levels).</p>
            <div className="space-y-1.5">
              {indicator.bands.map((band, i) => (
                <div key={i} className="flex items-center gap-2">
                  <Input
                    type="number"
                    value={band}
                    onChange={(e) =>
                      onChange({ bands: indicator.bands.map((b, idx) => (idx === i ? Number(e.target.value) : b)) })
                    }
                    className="flex-1"
                  />
                  <Button variant="ghost" size="sm" onClick={() => onChange({ bands: indicator.bands.filter((_, idx) => idx !== i) })}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
              <Button variant="secondary" size="sm" onClick={() => onChange({ bands: [...indicator.bands, 0] })}>
                <Plus className="h-3.5 w-3.5" /> Add band
              </Button>
            </div>
          </div>
        )}

        <div className="flex justify-end">
          <Button onClick={onClose}>Done</Button>
        </div>
      </div>
    </Modal>
  );
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
  const [indicatorLines, setIndicatorLines] = useState<Map<string, OverlayLine[]>>(new Map());
  const [settingsFor, setSettingsFor] = useState<string | null>(null);

  const { data: indicatorList } = useIndicatorList();
  const specByCode = useMemo(() => new Map((indicatorList ?? []).map((s) => [s.code, s] as const)), [indicatorList]);

  const [activeIndicators, setActiveIndicators] = useState<ActiveIndicator[]>([]);
  const hasSeededDefaults = useRef(false);
  useEffect(() => {
    if (hasSeededDefaults.current || !indicatorList) return;
    hasSeededDefaults.current = true;
    setActiveIndicators(
      DEFAULT_ACTIVE_CODES.filter((code) => specByCode.has(code)).map((code, i) => ({
        code,
        color: INDICATOR_COLORS[i % INDICATOR_COLORS.length],
        bands: DEFAULT_BANDS[code] ?? [],
        params: { ...(specByCode.get(code)?.default_params ?? {}) },
      })),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one-shot default seeding once the registry loads, guarded by the ref above
  }, [indicatorList]);

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
    if (!code || activeIndicators.some((a) => a.code === code)) return;
    const spec = specByCode.get(code);
    setActiveIndicators((prev) => [
      ...prev,
      {
        code,
        color: INDICATOR_COLORS[prev.length % INDICATOR_COLORS.length],
        bands: DEFAULT_BANDS[code] ?? [],
        params: { ...(spec?.default_params ?? {}) },
      },
    ]);
  }

  function removeIndicator(code: string) {
    setActiveIndicators((prev) => prev.filter((a) => a.code !== code));
    setIndicatorLines((prev) => {
      const next = new Map(prev);
      next.delete(code);
      return next;
    });
    setOscillatorChartApis((prev) => Object.fromEntries(Object.entries(prev).filter(([c]) => c !== code)));
  }

  function updateIndicator(code: string, patch: Partial<ActiveIndicator>) {
    setActiveIndicators((prev) => prev.map((a) => (a.code === code ? { ...a, ...patch } : a)));
  }

  const activeWithSpecs = activeIndicators
    .map((indicator) => ({ indicator, spec: specByCode.get(indicator.code) }))
    .filter((x): x is { indicator: ActiveIndicator; spec: IndicatorSpecOut } => !!x.spec);
  const overlayEntries = activeWithSpecs.filter((x) => x.spec.overlay);
  const oscillatorEntries = activeWithSpecs.filter((x) => !x.spec.overlay);
  const editing = settingsFor ? activeWithSpecs.find((x) => x.indicator.code === settingsFor) : undefined;

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

  const overlays: OverlayLine[] = overlayEntries.flatMap((x) => indicatorLines.get(x.indicator.code) ?? EMPTY_LINES);

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
      {resolvedSelected &&
        indicatorsAvailable &&
        activeWithSpecs.map(({ indicator, spec }) => (
          <IndicatorSeries
            key={indicator.code}
            instrumentId={resolvedSelected.id}
            timeframe={timeframe}
            baseTimeframe={effectiveBase}
            spec={spec}
            indicator={indicator}
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
                  const options = (indicatorList ?? []).filter(
                    (s) => s.category === category && !activeIndicators.some((a) => a.code === s.code),
                  );
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
          {resolvedSelected && activeWithSpecs.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {activeWithSpecs.map(({ indicator, spec }) => (
                <Badge key={indicator.code} style={{ backgroundColor: `${indicator.color}22`, color: indicator.color }}>
                  {spec.name}
                  <button onClick={() => setSettingsFor(indicator.code)} title="Settings">
                    <Settings2 className="h-3 w-3" />
                  </button>
                  <button onClick={() => removeIndicator(indicator.code)} title="Remove">
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
              {oscillatorEntries.map(({ indicator, spec }) => (
                <div key={indicator.code}>
                  <div className="mb-1 text-xs font-medium text-text-muted">{spec.name}</div>
                  <OscillatorChart
                    lines={indicatorLines.get(indicator.code) ?? EMPTY_LINES}
                    bands={indicator.bands.length ? indicator.bands : EMPTY_BANDS}
                    onChartReady={(chart) => setOscillatorChartApis((prev) => ({ ...prev, [indicator.code]: chart }))}
                  />
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {editing && (
        <IndicatorSettingsModal
          spec={editing.spec}
          indicator={editing.indicator}
          onChange={(patch) => updateIndicator(editing.indicator.code, patch)}
          onClose={() => setSettingsFor(null)}
        />
      )}
    </div>
  );
}
