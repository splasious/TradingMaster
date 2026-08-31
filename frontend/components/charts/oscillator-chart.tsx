"use client";

import { useEffect, useRef } from "react";
import { ColorType, LineSeries, createChart, type IChartApi, type UTCTimestamp } from "lightweight-charts";

interface OscillatorChartProps {
  points: { ts: string; value: number | null }[];
  color?: string;
  bands?: number[]; // horizontal reference lines, e.g. [30, 70] for RSI
  height?: number;
  onChartReady?: (chart: IChartApi | null) => void;
}

function toUnixSeconds(ts: string): UTCTimestamp {
  return Math.floor(new Date(ts).getTime() / 1000) as UTCTimestamp;
}

export function OscillatorChart({ points, color = "#3b6bf5", bands = [], height = 140, onChartReady }: OscillatorChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

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

    const series = chart.addSeries(LineSeries, { color, lineWidth: 1 });
    series.setData(points.filter((p) => p.value !== null).map((p) => ({ time: toUnixSeconds(p.ts), value: p.value as number })));

    for (const band of bands) {
      series.createPriceLine({ price: band, color: "#88909c", lineWidth: 1, lineStyle: 2, axisLabelVisible: true });
    }

    chart.timeScale().fitContent();

    const resizeObserver = new ResizeObserver((entries) => {
      chart.applyOptions({ width: entries[0].contentRect.width });
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      onChartReady?.(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, color, height]);

  return <div ref={containerRef} className="w-full" />;
}
