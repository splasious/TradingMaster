import type { IChartApi, LogicalRange } from "lightweight-charts";

/** Links the visible time range of multiple independent lightweight-charts
 * instances (e.g. a price chart and an RSI panel below it) so panning or
 * zooming one moves the others together. Each chart is its own createChart()
 * instance with no shared time scale by default -- this is what actually
 * keeps them visually aligned. Returns an unsubscribe function. */
export function syncChartTimeScales(charts: IChartApi[]): () => void {
  let syncing = false;

  const handlers = charts.map((chart, i) => {
    const handler = (range: LogicalRange | null) => {
      if (syncing || range === null) return;
      syncing = true;
      for (let j = 0; j < charts.length; j++) {
        if (j !== i) {
          try {
            charts[j].timeScale().setVisibleLogicalRange(range);
          } catch {
            // the other chart may already be mid-teardown -- nothing to sync to
          }
        }
      }
      syncing = false;
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(handler);
    return handler;
  });

  return () => {
    charts.forEach((chart, i) => {
      try {
        chart.timeScale().unsubscribeVisibleLogicalRangeChange(handlers[i]);
      } catch {
        // chart already removed
      }
    });
  };
}
