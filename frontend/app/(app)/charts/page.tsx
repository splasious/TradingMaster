"use client";

import { useMemo, useState } from "react";

import { OscillatorChart } from "@/components/charts/oscillator-chart";
import { PriceChart, type OverlayLine } from "@/components/charts/price-chart";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { useChartCandles, useIndicator, useInstruments } from "@/lib/hooks";
import type { InstrumentOut } from "@/lib/types";

const OVERLAY_OPTIONS = [
  { code: "sma", field: "sma", label: "SMA 20", color: "#3b6bf5" },
  { code: "ema", field: "ema", label: "EMA 50", color: "#f59e0b" },
] as const;

function useOverlay(instrumentId: string | null, timeframe: string, code: string, field: string, color: string, enabled: boolean): OverlayLine | null {
  const { data } = useIndicator(enabled ? instrumentId : null, timeframe, code);
  return useMemo(() => {
    if (!enabled || !data) return null;
    return { id: code, color, points: data.map((p) => ({ ts: p.ts, value: p.values[field] })) };
  }, [enabled, data, code, field, color]);
}

export default function ChartsPage() {
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<InstrumentOut | null>(null);
  const [timeframe, setTimeframe] = useState("1d");
  const [showSma, setShowSma] = useState(true);
  const [showEma, setShowEma] = useState(false);
  const [showBollinger, setShowBollinger] = useState(false);
  const [showRsi, setShowRsi] = useState(true);

  // Indicators are computed server-side against stored candles, so they're
  // only available at the base timeframe we actually backfill ("1d"). The
  // "1wk"/"1mo" views use the multi-timeframe resample endpoint for price
  // data but don't (yet) support indicator overlays on top of that.
  const indicatorsAvailable = timeframe === "1d";

  const { data: instruments } = useInstruments(q);
  const { data: candles, isLoading: candlesLoading } = useChartCandles(selected?.id ?? null, timeframe);
  const { data: rsi } = useIndicator(indicatorsAvailable && showRsi ? (selected?.id ?? null) : null, timeframe, "rsi");
  const { data: bollinger } = useIndicator(
    indicatorsAvailable && showBollinger ? (selected?.id ?? null) : null,
    timeframe,
    "bollinger_bands",
  );

  const smaLine = useOverlay(selected?.id ?? null, timeframe, "sma", "sma", "#3b6bf5", indicatorsAvailable && showSma);
  const emaLine = useOverlay(selected?.id ?? null, timeframe, "ema", "ema", "#f59e0b", indicatorsAvailable && showEma);

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
          <div className="max-h-[32rem] space-y-0.5 overflow-y-auto">
            {instruments?.map((i) => (
              <button
                key={i.id}
                onClick={() => setSelected(i)}
                className={`flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm ${
                  selected?.id === i.id ? "bg-active-soft text-active" : "text-text-secondary hover:bg-surface-elevated"
                }`}
              >
                {i.symbol} <span className="text-text-muted">({i.exchange})</span>
              </button>
            ))}
          </div>
        </CardContent>
      </Card>

      <Card className="lg:col-span-3">
        <CardHeader className="flex-wrap gap-3">
          <CardTitle>{selected ? `${selected.symbol} -- ${selected.name}` : "Select an instrument"}</CardTitle>
          {selected && (
            <div className="flex flex-wrap items-center gap-3">
              <Select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="w-24">
                {["1d", "1wk", "1mo"].map((tf) => (
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
              {!indicatorsAvailable && <span className="text-xs text-text-muted">(indicators available on 1d only)</span>}
            </div>
          )}
        </CardHeader>
        <CardContent>
          {!selected ? (
            <p className="py-16 text-center text-sm text-text-muted">Pick an instrument to view its chart.</p>
          ) : candlesLoading ? (
            <p className="py-16 text-center text-sm text-text-muted">Loading candles...</p>
          ) : !candles?.length ? (
            <p className="py-16 text-center text-sm text-text-muted">
              No candles stored for this timeframe yet -- run a backfill from Market Data first.
            </p>
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
