"use client";

import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";

import type { CandleOut } from "@/lib/types";

export interface OverlayLine {
  id: string;
  color: string;
  points: { ts: string; value: number | null }[];
}

/** Converts to lightweight-charts' own point shape, preserving every point
 * -- including an indicator's leading warmup-period nulls -- as
 * "whitespace" (a time slot with no plotted value) rather than dropping
 * them. Multiple independent chart instances (price + each oscillator
 * panel) are kept in sync via setVisibleLogicalRange, which aligns them by
 * bar *index*, not calendar time -- so if one series silently drops its
 * first N points (e.g. RSI's 14-bar warmup, MACD's ~34), its index 0 no
 * longer means the same date as every other panel's index 0, and panning
 * one desyncs the rest by exactly that many bars. Keeping the index
 * mapping identical to the price chart's own candle series is what makes
 * the sync correct, not just the subscription plumbing in chart-sync.ts. */
export function toSeriesPoints(points: { ts: string; value: number | null }[]): ({ time: UTCTimestamp; value: number } | { time: UTCTimestamp })[] {
  return points.map((p) => {
    const time = Math.floor(new Date(p.ts).getTime() / 1000) as UTCTimestamp;
    return p.value === null ? { time } : { time, value: p.value };
  });
}

interface PriceChartProps {
  candles: CandleOut[];
  overlays?: OverlayLine[];
  height?: number;
  onChartReady?: (chart: IChartApi | null) => void;
}

function toUnixSeconds(ts: string): UTCTimestamp {
  return Math.floor(new Date(ts).getTime() / 1000) as UTCTimestamp;
}

export function PriceChart({ candles, overlays = [], height = 420, onChartReady }: PriceChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const overlaySeriesRef = useRef<Map<string, ISeriesApi<"Line">>>(new Map());

  useEffect(() => {
    if (!containerRef.current) return;

    const isDark = document.documentElement.classList.contains("dark");
    const chart = createChart(containerRef.current, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: isDark ? "#a4acba" : "#545b68",
      },
      grid: {
        vertLines: { color: isDark ? "#1c2028" : "#eef0f3" },
        horzLines: { color: isDark ? "#1c2028" : "#eef0f3" },
      },
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    chartRef.current = chart;
    onChartReady?.(chart);

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#15803d",
      downColor: "#b91c1c",
      borderVisible: false,
      wickUpColor: "#15803d",
      wickDownColor: "#b91c1c",
    });
    candleSeriesRef.current = candleSeries;

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
    volumeSeriesRef.current = volumeSeries;

    const resizeObserver = new ResizeObserver((entries) => {
      const { width } = entries[0].contentRect;
      chart.applyOptions({ width });
    });
    resizeObserver.observe(containerRef.current);

    const overlaySeries = overlaySeriesRef.current;
    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      overlaySeries.clear();
      onChartReady?.(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [height]);

  useEffect(() => {
    if (!candleSeriesRef.current || !volumeSeriesRef.current) return;

    candleSeriesRef.current.setData(
      candles.map((c) => ({
        time: toUnixSeconds(c.ts),
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      })),
    );
    volumeSeriesRef.current.setData(
      candles.map((c) => ({
        time: toUnixSeconds(c.ts),
        value: c.volume ?? 0,
        color: c.close >= c.open ? "rgba(21,128,61,0.4)" : "rgba(185,28,28,0.4)",
      })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [candles]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;

    const currentIds = new Set(overlays.map((o) => o.id));
    for (const [id, series] of overlaySeriesRef.current) {
      if (!currentIds.has(id)) {
        chart.removeSeries(series);
        overlaySeriesRef.current.delete(id);
      }
    }

    for (const overlay of overlays) {
      let series = overlaySeriesRef.current.get(overlay.id);
      if (!series) {
        series = chart.addSeries(LineSeries, { color: overlay.color, lineWidth: 1 });
        overlaySeriesRef.current.set(overlay.id, series);
      }
      series.setData(toSeriesPoints(overlay.points));
    }
  }, [overlays]);

  return <div ref={containerRef} className="w-full" />;
}
