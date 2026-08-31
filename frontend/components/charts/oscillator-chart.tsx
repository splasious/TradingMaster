"use client";

import { useEffect, useRef } from "react";
import { ColorType, LineSeries, createChart, type IChartApi, type IPriceLine, type ISeriesApi } from "lightweight-charts";

import { toSeriesPoints, type OverlayLine } from "./price-chart";

interface OscillatorChartProps {
  lines: OverlayLine[];
  bands?: number[]; // horizontal reference lines, e.g. [30, 70] for RSI
  height?: number;
  onChartReady?: (chart: IChartApi | null) => void;
}

export function OscillatorChart({ lines, bands = [], height = 140, onChartReady }: OscillatorChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  const priceLinesRef = useRef<IPriceLine[]>([]);

  // Chart creation is intentionally isolated to `height` alone -- mirrors
  // PriceChart's split (create once vs. update data) -- so a fresh `lines`
  // or `bands` array reference (e.g. multiple active oscillator panels all
  // re-rendering whenever any one of them finishes loading) never tears
  // down and rebuilds the chart. A remount here previously fired
  // onChartReady(null) then onChartReady(chart) on every such render,
  // which broke syncChartTimeScales' subscription and got dramatically
  // worse with 2+ oscillator panels active at once.
  useEffect(() => {
    if (!containerRef.current) return;
    const isDark = document.documentElement.classList.contains("dark");
    const chart = createChart(containerRef.current, {
      height,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: isDark ? "#a4acba" : "#545b68" },
      grid: { vertLines: { color: isDark ? "#1c2028" : "#eef0f3" }, horzLines: { color: isDark ? "#1c2028" : "#eef0f3" } },
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    chartRef.current = chart;
    onChartReady?.(chart);

    const resizeObserver = new ResizeObserver((entries) => {
      chart.applyOptions({ width: entries[0].contentRect.width });
    });
    resizeObserver.observe(containerRef.current);

    const seriesMap = seriesRef.current;
    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesMap.clear();
      priceLinesRef.current = [];
      onChartReady?.(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [height]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;

    const currentIds = new Set(lines.map((l) => l.id));
    for (const [id, series] of seriesRef.current) {
      if (!currentIds.has(id)) {
        chart.removeSeries(series);
        seriesRef.current.delete(id);
      }
    }
    for (const line of lines) {
      let series = seriesRef.current.get(line.id);
      if (!series) {
        series = chart.addSeries(LineSeries, { color: line.color, lineWidth: 1, title: line.id });
        seriesRef.current.set(line.id, series);
      }
      series.setData(toSeriesPoints(line.points));
    }

    const anchorSeries = seriesRef.current.values().next().value;
    if (anchorSeries) {
      for (const priceLine of priceLinesRef.current) anchorSeries.removePriceLine(priceLine);
      priceLinesRef.current = bands.map((band) =>
        anchorSeries.createPriceLine({ price: band, color: "#88909c", lineWidth: 1, lineStyle: 2, axisLabelVisible: true }),
      );
    }

    chart.timeScale().fitContent();
  }, [lines, bands]);

  return <div ref={containerRef} className="w-full" />;
}
