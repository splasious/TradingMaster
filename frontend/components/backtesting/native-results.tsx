"use client";

import { useEffect, useState } from "react";

import { OscillatorChart } from "@/components/charts/oscillator-chart";
import type { NativeBacktestMetrics } from "@/lib/types";

/** The Advanced backtest result (agreed 6 Oct, step 1): grouped KPIs, the
 * day-by-day equity (open positions included, charges taken off) beside
 * NIFTY 50, its drawdown, and the monthly returns. Results saved before
 * these KPIs existed (no kpi_version) show their original five numbers. */

// Categorical slots 1 and 2 (dataviz reference palette, validated light and
// dark against the app's surfaces): the strategy and NIFTY 50.
const SERIES = { light: ["#2a78d6", "#eb6834"], dark: ["#3987e5", "#d95926"] };
// Diverging blue (gain) <-> red (loss) with a gray midpoint, three steps a side.
const DIVERGING = {
  light: { gain: ["#dbe8f8", "#a9c9ef", "#6fa3e3"], loss: ["#fbe0df", "#f3b0af", "#ea7c7b"], mid: "#f0efec" },
  dark: { gain: ["#1d2f47", "#23466f", "#2f5f9a"], loss: ["#472323", "#6d2c2c", "#933939"], mid: "#383835" },
};
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function useDarkMode(): boolean {
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const root = document.documentElement;
    const read = () => setDark(root.classList.contains("dark"));
    read();
    const observer = new MutationObserver(read);
    observer.observe(root, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);
  return dark;
}

const rupees = (value: number | null | undefined, sign = false) =>
  value == null ? "—" : `${sign && value > 0 ? "+" : value < 0 ? "−" : ""}₹${Math.abs(value).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
const pct = (value: number | null | undefined, sign = false) =>
  value == null ? "—" : `${sign && value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(2)}%`;
const axisRupees = (price: number) => rupees(price);
const axisPct = (price: number) => `${price.toFixed(1)}%`;
const num = (value: number | null | undefined, digits = 2) => (value == null ? "—" : value.toFixed(digits));
const tone = (value: number | null | undefined) => (value == null || value === 0 ? "" : value > 0 ? "text-positive" : "text-negative");

/** % below the running peak at each point (0 at a new high). */
function drawdownSeries(points: { ts: string; value: number }[]): { ts: string; value: number }[] {
  const out: { ts: string; value: number }[] = [];
  let peak = -Infinity;
  for (const { ts, value } of points) {
    peak = Math.max(peak, value);
    out.push({ ts, value: peak > 0 ? Math.round(((value - peak) / peak) * 10000) / 100 : 0 });
  }
  return out;
}

function hours(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value < 24) return `${value.toFixed(1)} h`;
  return `${(value / 24).toFixed(1)} days`;
}

function Headline({ label, value, sub, className = "" }: { label: string; value: string; sub?: string; className?: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface-elevated p-4">
      <div className="text-xs font-medium uppercase tracking-wide text-text-muted">{label}</div>
      <div className={`mt-1 font-financial text-2xl font-semibold ${className || "text-text-primary"}`}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-text-secondary">{sub}</div>}
    </div>
  );
}

function Group({ title, rows }: { title: string; rows: [string, string, string?][] }) {
  return (
    <div className="rounded-lg border border-border p-4">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-muted">{title}</h3>
      <dl className="space-y-1.5">
        {rows.map(([label, value, cls]) => (
          <div key={label} className="flex items-baseline justify-between gap-3 text-sm">
            <dt className="text-text-secondary">{label}</dt>
            <dd className={`font-financial font-medium ${cls || "text-text-primary"}`}>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function LegacyKpis({ metrics }: { metrics: NativeBacktestMetrics }) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Headline label="Net P&L (before charges)" value={rupees(metrics.net_pnl, true)} className={tone(metrics.net_pnl)} />
        <Headline label="Win Rate" value={pct(metrics.win_rate_pct)} />
        <Headline label="Total Trades" value={`${metrics.trade_count}`} />
        <Headline label="Best Trade" value={rupees(metrics.best_trade, true)} className={tone(metrics.best_trade)} />
        <Headline label="Worst Trade" value={rupees(metrics.worst_trade, true)} className={tone(metrics.worst_trade)} />
        <Headline label="Final Capital" value={rupees(metrics.final_capital)} />
      </div>
      <p className="rounded-md border border-border bg-surface-elevated px-3 py-2 text-sm text-text-secondary">
        This backtest ran before the full results existed. <strong className="text-text-primary">Run it again</strong> for charges,
        drawdown, Sharpe, the NIFTY comparison and monthly returns.
      </p>
    </div>
  );
}

function MonthlyReturns({ metrics, dark }: { metrics: NativeBacktestMetrics; dark: boolean }) {
  const months = metrics.monthly_returns ?? [];
  if (!months.length) return null;
  const palette = DIVERGING[dark ? "dark" : "light"];
  const byYear = new Map<number, Map<number, number | null>>();
  for (const m of months) {
    if (!byYear.has(m.year)) byYear.set(m.year, new Map());
    byYear.get(m.year)!.set(m.month, m.return_pct);
  }
  const yearly = new Map((metrics.yearly_returns ?? []).map((y) => [y.year, y.return_pct]));
  const fill = (value: number | null | undefined) => {
    if (value == null) return "transparent";
    const size = Math.abs(value);
    if (size < 0.5) return palette.mid;
    const step = size < 2 ? 0 : size < 5 ? 1 : 2;
    return value > 0 ? palette.gain[step] : palette.loss[step];
  };
  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold text-text-primary">Monthly returns</h3>
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full min-w-[720px] border-separate border-spacing-0.5 text-xs">
          <thead>
            <tr className="text-text-muted">
              <th className="px-2 py-1 text-left font-medium">Year</th>
              {MONTHS.map((m) => (
                <th key={m} className="px-1 py-1 text-right font-medium">{m}</th>
              ))}
              <th className="px-2 py-1 text-right font-medium">Year</th>
            </tr>
          </thead>
          <tbody>
            {[...byYear.entries()].map(([year, row]) => (
              <tr key={year}>
                <td className="px-2 py-1 font-medium text-text-secondary">{year}</td>
                {MONTHS.map((label, i) => {
                  const value = row.get(i + 1);
                  return (
                    <td
                      key={label}
                      className="rounded-sm px-1 py-1 text-right font-financial text-text-primary"
                      style={{ background: fill(value) }}
                      title={value == null ? undefined : `${label} ${year}: ${pct(value, true)}`}
                    >
                      {value == null ? "" : pct(value, true)}
                    </td>
                  );
                })}
                <td className="rounded-sm px-2 py-1 text-right font-financial font-semibold text-text-primary" style={{ background: fill(yearly.get(year)) }}>
                  {pct(yearly.get(year), true)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-1 text-xs text-text-muted">Blue: gain · red: loss · gray: within ±0.5%. Darker = bigger move (2%, 5%).</p>
    </div>
  );
}

export function NativeBacktestResults({
  metrics,
  equityCurve,
  initialCapital,
}: {
  metrics: NativeBacktestMetrics;
  equityCurve: [string, number][];
  initialCapital: number;
}) {
  const dark = useDarkMode();
  const full = metrics.kpi_version === 2;
  const [strategyColor, niftyColor] = SERIES[dark ? "dark" : "light"];
  const points = equityCurve.map(([ts, value]) => ({ ts, value }));

  if (!full) {
    return (
      <div className="space-y-6">
        <LegacyKpis metrics={metrics} />
        <div>
          <h3 className="mb-2 text-sm font-semibold text-text-primary">Equity (at each trade close)</h3>
          <OscillatorChart lines={[{ id: "equity", color: strategyColor, points }]} bands={[initialCapital]} height={220} priceFormatter={axisRupees} />
        </div>
      </div>
    );
  }

  const bench = metrics.benchmark;
  const drawdown = drawdownSeries(points);
  const niftyPoints = (metrics.benchmark_curve ?? []).map(([day, value]) => ({ ts: `${day}T10:00:00Z`, value }));
  const breakdown = metrics.charges_breakdown ?? {};
  const charges = metrics.charges_total ?? 0;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Headline label="Net P&L (after charges)" value={rupees(metrics.net_pnl, true)} sub={`Final ${rupees(metrics.final_capital)}`} className={tone(metrics.net_pnl)} />
        <Headline
          label="Return"
          value={pct(metrics.total_return_pct, true)}
          sub={metrics.cagr_pct != null ? `CAGR ${pct(metrics.cagr_pct, true)}` : "CAGR: needs 3+ months"}
          className={tone(metrics.total_return_pct)}
        />
        <Headline
          label="Max drawdown"
          value={metrics.max_drawdown_pct ? `−${metrics.max_drawdown_pct.toFixed(2)}%` : "0.00%"}
          sub={metrics.max_drawdown_amount ? `${rupees(-metrics.max_drawdown_amount)}${metrics.max_drawdown_recovered ? "" : " · not recovered"}` : undefined}
          className={metrics.max_drawdown_pct ? "text-negative" : ""}
        />
        <Headline label="Sharpe" value={num(metrics.sharpe_ratio)} sub={`Sortino ${num(metrics.sortino_ratio)}`} />
        <Headline
          label="Profit factor"
          value={metrics.profit_factor != null ? num(metrics.profit_factor) : metrics.no_losing_trades ? "No losses" : "—"}
          sub={`Win rate ${pct(metrics.win_rate_pct)}`}
        />
        <Headline
          label={`vs ${bench?.symbol ?? "NIFTY 50"}`}
          value={bench ? pct(bench.excess_return_pct, true) : "—"}
          sub={bench ? `NIFTY ${pct(bench.return_pct, true)}` : "No NIFTY candles for these dates"}
          className={tone(bench?.excess_return_pct)}
        />
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <Group
          title="Returns"
          rows={[
            ["Net P&L (after charges)", rupees(metrics.net_pnl, true), tone(metrics.net_pnl)],
            ["Gross P&L", rupees(metrics.gross_pnl, true), tone(metrics.gross_pnl)],
            ["Return", pct(metrics.total_return_pct, true), tone(metrics.total_return_pct)],
            ["CAGR", pct(metrics.cagr_pct, true), tone(metrics.cagr_pct)],
            ["Starting / final capital", `${rupees(metrics.initial_capital)} / ${rupees(metrics.final_capital)}`],
          ]}
        />
        <Group
          title="Risk"
          rows={[
            ["Max drawdown", metrics.max_drawdown_pct ? `−${metrics.max_drawdown_pct.toFixed(2)}% (${rupees(-(metrics.max_drawdown_amount ?? 0))})` : "0.00%", metrics.max_drawdown_pct ? "text-negative" : ""],
            ["Peak → lowest", metrics.drawdown_peak_date ? `${metrics.drawdown_peak_date} → ${metrics.drawdown_trough_date}` : "—"],
            ["Recovered", metrics.drawdown_peak_date ? (metrics.max_drawdown_recovered ? "Yes" : "Not yet") : "—"],
            ["Longest time below a peak", `${metrics.longest_drawdown_days ?? 0} days${metrics.in_drawdown_at_end ? " (still below)" : ""}`],
            ["Annual volatility", pct(metrics.annual_volatility_pct)],
          ]}
        />
        <Group
          title="Risk-adjusted"
          rows={[
            ["Sharpe (0% risk-free)", num(metrics.sharpe_ratio)],
            ["Sortino", num(metrics.sortino_ratio)],
            ["Calmar (CAGR ÷ max DD)", num(metrics.calmar_ratio)],
          ]}
        />
        <Group
          title="Trades (after charges)"
          rows={[
            ["Trades · win rate", `${metrics.trade_count} · ${pct(metrics.win_rate_pct)}`],
            ["Profit factor", metrics.profit_factor != null ? num(metrics.profit_factor) : metrics.no_losing_trades ? "No losing trades" : "—"],
            ["Average win / loss", `${rupees(metrics.avg_win, true)} / ${rupees(metrics.avg_loss, true)}`],
            ["Payoff ratio", num(metrics.payoff_ratio)],
            ["Expectancy per trade", rupees(metrics.expectancy, true), tone(metrics.expectancy)],
            ["Best / worst trade", `${rupees(metrics.best_trade, true)} / ${rupees(metrics.worst_trade, true)}`],
            ["Longest winning / losing streak", `${metrics.max_consecutive_wins ?? 0} / ${metrics.max_consecutive_losses ?? 0}`],
            ["Average holding", hours(metrics.avg_holding_hours)],
          ]}
        />
        <Group
          title="Costs (estimated)"
          rows={[
            ["Total charges", rupees(charges)],
            ["Brokerage", rupees(breakdown.brokerage)],
            ["STT", rupees(breakdown.stt)],
            ["Exchange + SEBI", rupees((breakdown.exchange ?? 0) + (breakdown.sebi ?? 0))],
            ["Stamp duty / GST", `${rupees(breakdown.stamp)} / ${rupees(breakdown.gst)}`],
            ["Charges as % of gross P&L", pct(metrics.charges_pct_of_gross)],
            ...((metrics.trades_without_charge_estimate ?? 0) > 0
              ? [["Trades with no estimate", `${metrics.trades_without_charge_estimate}`] as [string, string]]
              : []),
          ]}
        />
        <Group
          title={`Vs ${bench?.symbol ?? "NIFTY 50"} and consistency`}
          rows={[
            ["NIFTY return (buy & hold)", pct(bench?.return_pct, true), tone(bench?.return_pct)],
            ["NIFTY max drawdown", bench?.max_drawdown_pct ? `−${bench.max_drawdown_pct.toFixed(2)}%` : "—"],
            ["Strategy above NIFTY", pct(bench?.excess_return_pct, true), tone(bench?.excess_return_pct)],
            ["Days in the market", `${pct(metrics.exposure_pct)} of ${metrics.trading_days ?? 0} trading days`],
          ]}
        />
      </div>

      <div>
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-semibold text-text-primary">Equity, day by day (open positions included, after charges)</h3>
          <div className="flex items-center gap-4 text-xs text-text-secondary" aria-label="Legend">
            <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4 rounded" style={{ background: strategyColor }} />Strategy</span>
            {niftyPoints.length > 0 && (
              <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4 rounded" style={{ background: niftyColor }} />NIFTY 50 (same starting capital)</span>
            )}
          </div>
        </div>
        <OscillatorChart
          key={`equity-${dark}`}
          lines={[
            { id: "strategy", color: strategyColor, points },
            ...(niftyPoints.length ? [{ id: "nifty", color: niftyColor, points: niftyPoints }] : []),
          ]}
          bands={[initialCapital]}
          height={240}
          priceFormatter={axisRupees}
        />
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold text-text-primary">Drawdown — % below the previous peak</h3>
        <OscillatorChart key={`dd-${dark}`} lines={[{ id: "drawdown", color: dark ? "#e66767" : "#d03b3b", points: drawdown }]} bands={[0]} height={140} priceFormatter={axisPct} />
      </div>

      <MonthlyReturns metrics={metrics} dark={dark} />
    </div>
  );
}
