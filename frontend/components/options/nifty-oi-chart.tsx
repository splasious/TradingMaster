"use client";

import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  type AutoscaleInfo,
  type IChartApi,
  type IPriceLine,
  type IPrimitivePaneRenderer,
  type IPrimitivePaneView,
  type ISeriesApi,
  type ISeriesPrimitive,
  type SeriesAttachedParameter,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useTheme } from "next-themes";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  arrow,
  buildLevels,
  fmtStrike,
  istDay,
  movingAverage,
  nextForming,
  periodStart,
  previousClose,
  toCandle,
  type Candle,
  type Forming,
  type Levels,
  type OiLevel,
} from "@/components/options/nifty-oi-model";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { useOiChart } from "@/lib/hooks";
import type { OiChartOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useMarketDataSocket } from "@/lib/ws";

/** Options > PCR Analysis: NIFTY 50's 15-minute candles, the one forming moved
 * tick by tick, with an expiry's top three call OI strikes above NIFTY
 * (resistance) and put OI strikes below it (support) as bars at their
 * prices -- the Telegram OI-levels picture, live. A bar's length is the
 * strike's OI now; its dotted outline, the OI at the latest 15-minute
 * record. Live OI streams for every contract the record captured, so a
 * level that changes hands shows as soon as it does. Outside market hours
 * it shows the latest record against the one before it, as the picture
 * does. */

const IST_OFFSET_SECONDS = 5.5 * 60 * 60;
const NEAR_LEVELS = 300; // a level farther than this beyond the candles' range is pinned at the edge, not scaled in
const SPOT_REACH = 100; // the chart shows at least this far either side of NIFTY when the levels are on
const FLUSH_MS = 500;
const timeFmt = new Intl.DateTimeFormat("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
const dayFmt = new Intl.DateTimeFormat("en-IN", { weekday: "short", day: "2-digit", month: "short", timeZone: "Asia/Kolkata" });
const expiryFmt = new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", timeZone: "UTC" });

function displayTime(startMs: number): UTCTimestamp {
  return (Math.floor(startMs / 1000) + IST_OFFSET_SECONDS) as UTCTimestamp;
}

interface Colours {
  text: string;
  text2: string;
  muted: string;
  grid: string;
  surface: string;
  call: string;
  put: string;
  spot: string;
  ma: string;
}

function readColours(): Colours {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  return {
    text: v("--text-primary", "#12151a"),
    text2: v("--text-secondary", "#545b68"),
    muted: v("--text-muted", "#88909c"),
    grid: v("--border", "#e2e5eb"),
    surface: v("--surface", "#ffffff"),
    call: v("--oi-call", "#c42f2f"),
    put: v("--oi-put", "#0a8a0a"),
    spot: v("--oi-spot", "#c27c00"),
    ma: v("--oi-ma", "#2a78d6"),
  };
}

function withAlpha(hex: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

// ---------------------------------------------------------------- the OI bars

interface BarsState {
  levels: Levels | null;
  show: boolean;
  colours: Colours;
  strip: number; // the right-hand share of the pane the bars take
  compact: boolean;
  step: number; // strike step, points
  lo: number; // the candles' low and high
  hi: number;
  sessions: UTCTimestamp[]; // each session's first candle after the first one shown
  sessionLabels: string[];
}

export interface Hit {
  x: number;
  y: number;
  w: number;
  h: number;
  level: OiLevel;
}

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];
type Ctx = CanvasRenderingContext2D;

function roundRect(ctx: Ctx, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, Math.min(r, h / 2, Math.abs(w) / 2));
}

/** Draws on the candle series' pane: behind the candles, the band between S1
 * and R1 and a dotted line where a session starts; in front, the six bars
 * in a strip of their own right of the forming candle, growing left from
 * the edge, each labelled with its tag, strike, OI and change. */
class OiBars implements ISeriesPrimitive<Time> {
  state: BarsState;
  hits: Hit[] = [];
  private series: ISeriesApi<"Candlestick"> | null = null;
  private chart: IChartApi | null = null;
  private requestUpdate: (() => void) | null = null;
  private readonly views: readonly IPrimitivePaneView[];

  constructor(state: BarsState) {
    this.state = state;
    const back: IPrimitivePaneRenderer = { draw: () => {}, drawBackground: (t) => this.drawBack(t) };
    const front: IPrimitivePaneRenderer = { draw: (t) => this.drawFront(t) };
    this.views = [
      { zOrder: () => "bottom", renderer: () => back },
      { zOrder: () => "top", renderer: () => front },
    ];
  }

  attached({ chart, series, requestUpdate }: SeriesAttachedParameter<Time>) {
    this.chart = chart as IChartApi;
    this.series = series as ISeriesApi<"Candlestick">;
    this.requestUpdate = requestUpdate;
  }

  detached() {
    this.chart = null;
    this.series = null;
    this.requestUpdate = null;
  }

  update(next: Partial<BarsState>) {
    this.state = { ...this.state, ...next };
    this.requestUpdate?.();
  }

  paneViews() {
    return this.views;
  }

  autoscaleInfo(): AutoscaleInfo | null {
    const { levels, show, lo, hi } = this.state;
    if (!show || !levels || !Number.isFinite(lo)) return null;
    const near = [...levels.resistance, ...levels.support]
      .map((l) => l.strike)
      .filter((s) => s >= lo - NEAR_LEVELS && s <= hi + NEAR_LEVELS);
    const all = [...near, levels.spot - SPOT_REACH, levels.spot + SPOT_REACH];
    return { priceRange: { minValue: Math.min(...all), maxValue: Math.max(...all) } };
  }

  private y(price: number): number | null {
    return this.series?.priceToCoordinate(price) ?? null;
  }

  private drawBack(target: Target) {
    const { levels, show, colours, strip, sessions, sessionLabels } = this.state;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const candlesRight = show ? mediaSize.width * (1 - strip) : mediaSize.width;
      ctx.save();
      ctx.setLineDash([2, 3]);
      ctx.strokeStyle = colours.muted;
      ctx.lineWidth = 1;
      ctx.font = "10px var(--font-geist-sans), system-ui, sans-serif";
      ctx.fillStyle = colours.muted;
      ctx.textBaseline = "top";
      const spacing = this.chart?.timeScale().options().barSpacing ?? 6;
      sessions.forEach((time, i) => {
        const at = this.chart?.timeScale().timeToCoordinate(time);
        if (at == null) return;
        const x = at - spacing / 2;
        if (x < 0 || x > candlesRight - 8) return;
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, 0);
        ctx.lineTo(Math.round(x) + 0.5, mediaSize.height);
        ctx.stroke();
        if (x + 90 < candlesRight) ctx.fillText(sessionLabels[i] ?? "", x + 4, 4);
      });
      if (show && levels?.resistance[0] && levels.support[0]) {
        const yr = this.y(levels.resistance[0].strike);
        const ys = this.y(levels.support[0].strike);
        if (yr != null && ys != null) {
          ctx.fillStyle = withAlpha(colours.ma, 0.08);
          ctx.fillRect(0, Math.min(yr, ys), candlesRight - 6, Math.abs(ys - yr));
        }
      }
      if (show) {
        ctx.beginPath();
        ctx.moveTo(Math.round(candlesRight) + 0.5, 0);
        ctx.lineTo(Math.round(candlesRight) + 0.5, mediaSize.height);
        ctx.stroke();
      }
      ctx.restore();
    });
  }

  private drawFront(target: Target) {
    const { levels, show, colours, strip, compact, step } = this.state;
    this.hits = [];
    if (!show || !levels) return;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const left = mediaSize.width * (1 - strip) + 6;
      const right = mediaSize.width - 4;
      const maxBar = (right - left) * (compact ? 0.3 : 0.5);
      const bars = [...levels.resistance, ...levels.support];
      const top = Math.max(1e-9, ...bars.map((b) => Math.max(b.oi, b.ref ?? 0)));
      const y0 = this.y(levels.spot);
      const y1 = this.y(levels.spot + step);
      const perStep = y0 != null && y1 != null ? Math.abs(y0 - y1) : 20;
      const barH = Math.max(7, Math.min(18, perStep * 0.62));
      const font = compact ? 10 : 11;
      ctx.save();
      ctx.textBaseline = "middle";

      for (const [side, list] of [["CE", levels.resistance], ["PE", levels.support]] as const) {
        const colour = side === "CE" ? colours.call : colours.put;
        // Where each bar sits; one beyond the pane is pinned at its edge, an arrow saying which way.
        const placed = list
          .map((level) => {
            const y = this.y(level.strike);
            if (y == null) return null;
            const above = y < 10;
            const below = y > mediaSize.height - 10;
            return { level, y: Math.max(10, Math.min(mediaSize.height - 10, y)), pinned: above ? "↑" : below ? "↓" : "" };
          })
          .filter((p): p is { level: OiLevel; y: number; pinned: string } => p != null)
          .sort((a, b) => a.y - b.y);
        // Labels at least a line apart, nudged off each other.
        const gap = font + 5;
        for (let i = 1; i < placed.length; i++) placed[i].y = Math.max(placed[i].y, placed[i - 1].y + gap);
        for (let i = placed.length - 2; i >= 0; i--) {
          if (placed[i + 1].y > mediaSize.height - 10) placed[i + 1].y = mediaSize.height - 10;
          placed[i].y = Math.min(placed[i].y, placed[i + 1].y - gap);
        }

        for (const { level, y, pinned } of placed) {
          const len = Math.max(2, (maxBar * level.oi) / top);
          const refLen = level.ref != null ? (maxBar * level.ref) / top : 0;
          ctx.globalAlpha = level.rank === 1 ? 0.95 : 0.7;
          ctx.fillStyle = colour;
          roundRect(ctx, right - len, y - barH / 2, len, barH, 3);
          ctx.fill();
          ctx.globalAlpha = 1;
          if (level.ref != null) {
            ctx.setLineDash([2, 2]);
            ctx.strokeStyle = colours.text2;
            ctx.lineWidth = 1;
            ctx.strokeRect(Math.round(right - refLen) + 0.5, Math.round(y - barH / 2) + 0.5, Math.round(refLen), Math.round(barH));
            ctx.setLineDash([]);
          }

          // Label, right to left: the OI and its change, then the tag chip.
          const barStart = right - Math.max(len, refLen);
          const value = compact ? `${level.oi.toFixed(1)}` : `${level.oi.toFixed(1)} L ${arrow(level.change)}`;
          ctx.font = `${level.rank === 1 ? 600 : 400} ${font}px var(--font-geist-sans), system-ui, sans-serif`;
          const valueW = ctx.measureText(value).width;
          const valueX = barStart - 6;
          ctx.fillStyle = colours.surface;
          ctx.fillRect(valueX - valueW - 3, y - (font + 2) / 2, valueW + 6, font + 2);
          ctx.fillStyle = colours.text;
          ctx.textAlign = "right";
          ctx.fillText(value, valueX, y);
          const chip = `${pinned}${level.tag} ${fmtStrike(level.strike)}`;
          ctx.font = `600 ${font}px var(--font-geist-sans), system-ui, sans-serif`;
          const chipW = ctx.measureText(chip).width + 8;
          const chipX = valueX - valueW - 6 - chipW;
          ctx.fillStyle = colour;
          roundRect(ctx, chipX, y - (font + 4) / 2, chipW, font + 4, 3);
          ctx.fill();
          ctx.fillStyle = colours.surface;
          ctx.textAlign = "left";
          ctx.fillText(chip, chipX + 4, y + 0.5);
          this.hits.push({ x: chipX, y: y - Math.max(barH, font + 4) / 2, w: right - chipX, h: Math.max(barH, font + 4), level });
        }
      }
      ctx.restore();
    });
  }
}

// ---------------------------------------------------------------- the chart

interface ChartProps {
  data: OiChartOut;
  levels: Levels | null;
  show: boolean;
  livePrice: number | null;
}

function LiveCandleChart({ data, levels, show, livePrice }: ChartProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const maRef = useRef<ISeriesApi<"Line"> | null>(null);
  const barsRef = useRef<OiBars | null>(null);
  const linesRef = useRef<IPriceLine[]>([]);
  const formingRef = useRef<Forming>({ forming: null, done: [] });
  const shownRef = useRef<Candle[]>([]);
  const showRef = useRef(show);
  const [hover, setHover] = useState<{ hit: Hit; x: number; y: number } | null>(null);

  // Candle spacing so the candles fill the left of the pane and the bars the right.
  const layout = () => {
    const chart = chartRef.current;
    const bars = barsRef.current;
    if (!chart || !bars) return;
    const width = chart.timeScale().width();
    if (!width) return;
    const compact = width < 520;
    const strip = showRef.current ? (compact ? 0.52 : 0.36) : 0.03;
    const n = Math.max(1, shownRef.current.length);
    const spacing = Math.max(2, Math.min(22, (width * (1 - strip) - 12) / n));
    chart.timeScale().applyOptions({ barSpacing: spacing, rightOffset: (width * strip) / spacing + 0.5 });
    chart.timeScale().scrollToRealTime();
    const sessions: UTCTimestamp[] = [];
    const labels: string[] = [];
    shownRef.current.forEach((c, i) => {
      if (i === 0 || istDay(c.start) === istDay(shownRef.current[i - 1].start)) return;
      sessions.push(displayTime(c.start));
      labels.push(dayFmt.format(new Date(c.start)));
    });
    bars.update({ strip, compact, sessions, sessionLabels: labels });
  };

  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const colours = readColours();
    const chart = createChart(box, {
      height: box.clientHeight || 420,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: colours.text2, fontSize: 11 },
      grid: { vertLines: { visible: false }, horzLines: { color: withAlpha(colours.grid.length === 7 ? colours.grid : "#888888", 0.6) } },
      crosshair: { mode: CrosshairMode.Normal },
      localization: {
        priceFormatter: (p: number) => p.toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
        tickmarksPriceFormatter: (prices: number[]) => prices.map((p) => p.toLocaleString("en-IN", { maximumFractionDigits: 0 })),
      },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.06, bottom: 0.06 } },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, shiftVisibleRangeOnNewBar: true },
      handleScroll: false,
      handleScale: false,
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: "rgba(0,0,0,0)",
      downColor: colours.call,
      borderUpColor: colours.put,
      borderDownColor: colours.call,
      wickUpColor: colours.put,
      wickDownColor: colours.call,
      priceLineColor: colours.spot,
      priceLineStyle: LineStyle.Dotted,
      priceLineWidth: 1,
      priceFormat: { type: "price", precision: 1, minMove: 0.05 },
    });
    const ma = chart.addSeries(LineSeries, {
      color: colours.ma,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    const bars = new OiBars({
      levels: null, show: showRef.current, colours, strip: 0.36, compact: false, step: 50, lo: NaN, hi: NaN, sessions: [], sessionLabels: [],
    });
    candles.attachPrimitive(bars);
    chartRef.current = chart;
    candlesRef.current = candles;
    maRef.current = ma;
    barsRef.current = bars;

    chart.subscribeCrosshairMove((param) => {
      const point = param.point;
      const hit = point && barsRef.current?.hits.find((h) => point.x >= h.x && point.x <= h.x + h.w && point.y >= h.y && point.y <= h.y + h.h);
      setHover(hit && point ? { hit, x: point.x, y: point.y } : null);
    });

    const resize = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      chart.applyOptions({ width, height });
      layout();
    });
    resize.observe(box);
    return () => {
      resize.disconnect();
      chart.remove();
      chartRef.current = null;
      candlesRef.current = null;
      maRef.current = null;
      barsRef.current = null;
      linesRef.current = [];
    };
  }, []);

  // Candles: the saved ones, any finished here since, and the one forming at the live price.
  useEffect(() => {
    const candles = candlesRef.current;
    const ma = maRef.current;
    const bars = barsRef.current;
    if (!candles || !ma || !bars) return;
    const start = data.market_open ? periodStart(Date.now()) : null;
    formingRef.current = nextForming(formingRef.current, data, livePrice, start);
    const { forming, done } = formingRef.current;
    const shown = [...data.candles.map(toCandle), ...done, ...(forming ? [forming] : [])];
    shownRef.current = shown;
    const faded = bars.state.colours;
    candles.setData(
      shown.map((c) => {
        const point = { time: displayTime(c.start), open: c.open, high: c.high, low: c.low, close: c.close };
        if (c !== forming) return point;
        const colour = withAlpha(c.close >= c.open ? faded.put : faded.call, 0.5);
        return { ...point, color: c.close >= c.open ? "rgba(0,0,0,0)" : colour, borderColor: colour, wickColor: colour };
      }),
    );
    const avg = movingAverage(shown);
    ma.setData(shown.map((c, i) => (avg[i] == null ? { time: displayTime(c.start) } : { time: displayTime(c.start), value: avg[i]! })));
    bars.update({
      lo: Math.min(...shown.map((c) => c.low)),
      hi: Math.max(...shown.map((c) => c.high)),
      step: data.strike_step ?? 50,
    });
    layout();
  }, [data, livePrice]);

  // The levels: bars, and R1 / S1 dashed across the candles.
  useEffect(() => {
    const candles = candlesRef.current;
    const bars = barsRef.current;
    if (!candles || !bars) return;
    showRef.current = show;
    bars.update({ levels, show });
    for (const line of linesRef.current) candles.removePriceLine(line);
    linesRef.current = [];
    if (show && levels) {
      const colours = bars.state.colours;
      for (const [level, colour] of [[levels.resistance[0], colours.call], [levels.support[0], colours.put]] as const) {
        if (!level) continue;
        linesRef.current.push(
          candles.createPriceLine({
            price: level.strike, color: colour, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true,
          }),
        );
      }
    }
    layout();
  }, [levels, show]);

  return (
    <div className="relative">
      <div ref={boxRef} className="h-[360px] w-full sm:h-[440px]" />
      {hover && <LevelTip hit={hover.hit} x={hover.x} y={hover.y} spot={levels?.spot ?? null} live={levels?.live ?? false} data={data} />}
    </div>
  );
}

function LevelTip({ hit, x, y, spot, live, data }: { hit: Hit; x: number; y: number; spot: number | null; live: boolean; data: OiChartOut }) {
  const l = hit.level;
  const since = live ? data.record_ts : data.prev_ts;
  return (
    <div
      className="pointer-events-none absolute z-10 w-56 rounded-md border border-border bg-surface-elevated p-2.5 text-xs shadow-lg"
      style={{ left: Math.max(4, x - 236), top: Math.max(4, y - 40) }}
    >
      <div className="mb-1 flex items-center justify-between font-semibold text-text-primary">
        <span>
          <span className={cn("mr-1.5 inline-block h-2 w-2 rounded-sm", l.side === "CE" ? "bg-oi-call" : "bg-oi-put")} aria-hidden />
          {l.tag} · {fmtStrike(l.strike)} {l.side}
        </span>
        <span className="font-normal text-text-muted">{l.side === "CE" ? "resistance" : "support"}</span>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-text-secondary">
        <dt>OI {l.live ? "now" : live ? "(no live tick)" : "at the record"}</dt>
        <dd className="text-right font-financial text-text-primary">{l.oi.toFixed(2)} L</dd>
        <dt>since {since ? timeFmt.format(new Date(since)) : "the previous"} record</dt>
        <dd className="text-right font-financial text-text-primary">{arrow(l.change, 2)} L</dd>
        <dt>since the previous close</dt>
        <dd className="text-right font-financial text-text-primary">{arrow(l.day, 2)} L</dd>
        {spot != null && (
          <>
            <dt>from NIFTY</dt>
            <dd className="text-right font-financial text-text-primary">{(l.strike - spot > 0 ? "+" : "") + (l.strike - spot).toFixed(0)} pts</dd>
          </>
        )}
      </dl>
    </div>
  );
}

// ---------------------------------------------------------------- the card

function LevelTable({ title, list, spot, colour }: { title: string; list: OiLevel[]; spot: number; colour: "call" | "put" }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 text-xs font-semibold text-text-primary">{title}</div>
      <table className="w-full text-xs">
        <thead className="text-text-muted">
          <tr>
            <th className="py-0.5 text-left font-normal">Strike</th>
            <th className="py-0.5 text-right font-normal">OI (L)</th>
            <th className="py-0.5 text-right font-normal">Δ</th>
            <th className="py-0.5 text-right font-normal">Δday</th>
            <th className="py-0.5 text-right font-normal">Away</th>
          </tr>
        </thead>
        <tbody className="font-financial text-text-primary">
          {list.map((l) => (
            <tr key={l.tag} className="border-t border-border">
              <td className="py-1">
                <span
                  className={cn(
                    "mr-1.5 inline-block rounded px-1 py-px text-[10px] font-semibold text-surface",
                    colour === "call" ? "bg-oi-call" : "bg-oi-put",
                  )}
                >
                  {l.tag}
                </span>
                {fmtStrike(l.strike)}
              </td>
              <td className={cn("py-1 text-right", l.rank === 1 && "font-semibold")}>{l.oi.toFixed(1)}</td>
              <td className="py-1 text-right">{arrow(l.change)}</td>
              <td className="py-1 text-right">{arrow(l.day)}</td>
              <td className="py-1 text-right text-text-secondary">{(l.strike - spot > 0 ? "+" : "") + (l.strike - spot).toFixed(0)}</td>
            </tr>
          ))}
          {!list.length && (
            <tr>
              <td colSpan={5} className="py-1 text-text-muted">none in the captured strikes</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function Swatch({ className, label, style }: { className?: string; label: string; style?: React.CSSProperties }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn("inline-block h-2.5 w-3.5 rounded-sm", className)} style={style} aria-hidden />
      {label}
    </span>
  );
}

export function NiftyOiChart() {
  const [expiry, setExpiry] = useState<string | null>(null);
  const [show, setShow] = useState(true);
  const { data, isLoading, isError } = useOiChart("NIFTY", expiry);
  const { resolvedTheme } = useTheme();

  const ids = useMemo(() => {
    if (!data?.market_open || !data.nifty_instrument_id) return [];
    return [data.nifty_instrument_id, ...data.contracts.flatMap((c) => (c.instrument_id ? [c.instrument_id] : []))];
  }, [data]);
  const { prices, status } = useMarketDataSocket(ids, { flushMs: FLUSH_MS });

  const niftyTick = data?.nifty_instrument_id ? prices[data.nifty_instrument_id] : undefined;
  const livePrice = data?.market_open ? (niftyTick && niftyTick.source === "kite" ? niftyTick.price : data.live_price) : null;
  const levels = useMemo(() => (data ? buildLevels(data, prices, livePrice) : null), [data, prices, livePrice]);

  const candles = useMemo(() => (data ? data.candles.map(toCandle) : []), [data]);
  const prevClose = useMemo(() => {
    const all = data?.forming ? [...candles, toCandle(data.forming)] : candles;
    return previousClose(all);
  }, [candles, data]);
  const price = livePrice ?? candles.at(-1)?.close ?? null;
  const change = price != null && prevClose != null ? price - prevClose : null;

  if (isLoading) return null;
  if (isError || !data) return null;
  if (!data.candles.length && !data.forming) return null;

  const r1 = levels?.resistance[0];
  const s1 = levels?.support[0];
  const statusText = data.market_open
    ? livePrice != null && status === "connected"
      ? `Live · ${levels?.liveContracts ?? 0}/${data.contracts.length} contracts ticking`
      : "Waiting for live ticks"
    : `Market closed · ${data.record_ts ? `${timeFmt.format(new Date(data.record_ts))} record` : "no record"}`;
  const refText = levels?.live
    ? `OI at the ${data.record_ts ? timeFmt.format(new Date(data.record_ts)) : "latest"} record`
    : `OI at the ${data.prev_ts ? timeFmt.format(new Date(data.prev_ts)) : "previous"} record`;

  return (
    <Card>
      <CardHeader className="items-start">
        <div className="min-w-0 space-y-1">
          <CardTitle className="flex flex-wrap items-center gap-x-2">
            NIFTY 50 · 15 min
            <span className="inline-flex items-center gap-1.5 text-xs font-normal text-text-secondary">
              <span
                className={cn(
                  "h-2 w-2 rounded-full",
                  data.market_open ? (livePrice != null && status === "connected" ? "bg-positive" : "bg-warning") : "bg-inactive",
                )}
                aria-hidden
              />
              {statusText}
            </span>
          </CardTitle>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
            <span className="font-financial text-2xl font-semibold text-text-primary">
              {price != null ? price.toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : "—"}
            </span>
            {change != null && prevClose != null && (
              <span className={cn("font-financial text-sm", change > 0 ? "text-positive" : change < 0 ? "text-negative" : "text-text-secondary")}>
                {change > 0 ? "+" : ""}
                {change.toFixed(1)} ({change > 0 ? "+" : ""}
                {((change / prevClose) * 100).toFixed(2)}%)
              </span>
            )}
            {data.record_pcr != null && <span className="text-xs text-text-secondary">PCR {data.record_pcr.toFixed(2)}</span>}
          </div>
        </div>
        <div className="flex flex-col items-end gap-2">
          <div className="flex flex-wrap items-center justify-end gap-2">
            {data.expiries.length > 0 && (
              <Select
                value={data.expiry ?? ""}
                onChange={(e) => setExpiry(e.target.value === data.expiries[0] ? null : e.target.value)}
                className="h-8 w-auto"
                aria-label="Expiry"
              >
                {data.expiries.map((e, i) => (
                  <option key={e} value={e}>
                    {expiryFmt.format(new Date(e))} expiry{i === 0 ? " (nearest)" : ""}
                  </option>
                ))}
              </Select>
            )}
            <Button
              size="sm"
              variant={show ? "primary" : "secondary"}
              aria-pressed={show}
              onClick={() => setShow((v) => !v)}
              disabled={!levels}
            >
              Top 3 OI
            </Button>
          </div>
          {show && r1 && s1 && (
            <div className="flex items-center gap-2 font-financial text-sm font-semibold">
              <span className="text-oi-put">S {fmtStrike(s1.strike)}</span>
              <span className="text-text-muted">·</span>
              <span className="text-oi-call">R {fmtStrike(r1.strike)}</span>
              <span className="text-xs font-normal text-text-secondary">range {fmtStrike(r1.strike - s1.strike)} pts</span>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3 px-2 pt-2 sm:px-4">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-2 text-xs text-text-secondary">
          {show && levels && (
            <>
              <Swatch className="bg-oi-call" label="Resistance (call OI)" />
              <Swatch className="bg-oi-put" label="Support (put OI)" />
              <Swatch className="border border-dashed border-text-secondary" label={refText} />
            </>
          )}
          <Swatch className="h-0.5 bg-oi-spot" label="NIFTY now" />
          <Swatch className="h-0.5" style={{ background: "var(--oi-ma)" }} label="8-candle MA" />
          {data.market_open && <Swatch className="border border-dashed border-text-muted" label="candle forming" />}
        </div>
        <LiveCandleChart key={resolvedTheme} data={data} levels={levels} show={show && levels != null} livePrice={livePrice} />
        {show && levels && (
          <div className="grid grid-cols-1 gap-4 px-2 sm:grid-cols-2">
            <LevelTable title="Resistance · call OI above NIFTY" list={levels.resistance} spot={levels.spot} colour="call" />
            <LevelTable title="Support · put OI below NIFTY" list={levels.support} spot={levels.spot} colour="put" />
          </div>
        )}
        <p className="px-2 text-[11px] text-text-muted">
          {data.expiry ? `${expiryFmt.format(new Date(data.expiry))} expiry · ` : ""}OI in lakh · bar = OI {levels?.live ? "now (live ticks)" : "at the record"},
          dotted outline = {refText.replace("OI at the", "the")} · Δ since then · Δday since the previous close · Away = points from NIFTY
        </p>
      </CardContent>
    </Card>
  );
}
