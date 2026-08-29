"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { OscillatorChart } from "@/components/charts/oscillator-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import { useBacktestJob, useBacktestResult, useBacktestTrades, useInstruments, useStrategies } from "@/lib/hooks";
import type { BacktestJobOut, BacktestMetrics, InstrumentOut, StrategyOut } from "@/lib/types";

function KpiTile({ label, value, tone = "neutral" }: { label: string; value: string; tone?: "positive" | "negative" | "neutral" }) {
  const toneClass = tone === "positive" ? "text-positive" : tone === "negative" ? "text-negative" : "text-text-primary";
  return (
    <div className="rounded-lg border border-border bg-surface-elevated p-4">
      <div className="text-xs font-medium uppercase tracking-wide text-text-muted">{label}</div>
      <div className={`mt-1 font-financial text-2xl font-semibold ${toneClass}`}>{value}</div>
    </div>
  );
}

function KpiGrid({ metrics }: { metrics: BacktestMetrics }) {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
      <KpiTile label="Net Profit" value={metrics.net_profit.toLocaleString(undefined, { maximumFractionDigits: 0, signDisplay: "always" })} tone={metrics.net_profit >= 0 ? "positive" : "negative"} />
      <KpiTile label="Win Rate" value={`${metrics.win_rate_pct}%`} />
      <KpiTile label="Profit Factor" value={`${metrics.profit_factor}`} tone={metrics.profit_factor >= 1 ? "positive" : "negative"} />
      <KpiTile label="Max Drawdown" value={`-${Math.abs(metrics.max_drawdown_pct)}%`} tone="negative" />
      <KpiTile label="Sharpe Ratio" value={`${metrics.sharpe_ratio}`} tone={metrics.sharpe_ratio >= 0 ? "positive" : "negative"} />
      <KpiTile label="Total Trades" value={`${metrics.num_trades}`} />
    </div>
  );
}

function RiskBar({ label, valuePct, max, tone }: { label: string; valuePct: number; max: number; tone: "positive" | "warning" | "negative" }) {
  const width = Math.min(100, Math.max(0, (Math.abs(valuePct) / max) * 100));
  const barToneClass = tone === "positive" ? "bg-positive" : tone === "warning" ? "bg-warning" : "bg-negative";
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-xs">
        <span className="font-medium uppercase tracking-wide text-text-muted">{label}</span>
        <span className="font-financial font-medium text-text-primary">{valuePct}%</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-border">
        <div className={`h-full rounded-full ${barToneClass}`} style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}

function RiskAnalytics({ metrics }: { metrics: BacktestMetrics }) {
  return (
    <div className="space-y-3 rounded-lg border border-border bg-surface-elevated p-4">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-text-muted">Risk Analytics</h3>
      <RiskBar label="Max Drawdown" valuePct={metrics.max_drawdown_pct} max={50} tone="negative" />
      <RiskBar label="Loss Rate" valuePct={metrics.loss_rate_pct} max={100} tone="warning" />
    </div>
  );
}

function MetricsGrid({ metrics, title }: { metrics: BacktestMetrics; title: string }) {
  const rows: [string, string][] = [
    ["Net Profit", metrics.net_profit.toLocaleString(undefined, { maximumFractionDigits: 2 })],
    ["Total Return", `${metrics.total_return_pct}%`],
    ["CAGR", `${metrics.cagr_pct}%`],
    ["Max Drawdown", `${metrics.max_drawdown_pct}%`],
    ["Sharpe Ratio", `${metrics.sharpe_ratio}`],
    ["Sortino Ratio", `${metrics.sortino_ratio}`],
    ["Profit Factor", `${metrics.profit_factor}`],
    ["Win Rate", `${metrics.win_rate_pct}%`],
    ["Trades", `${metrics.num_trades}`],
    ["Avg Win / Avg Loss", `${metrics.avg_win.toFixed(2)} / ${metrics.avg_loss.toFixed(2)}`],
    ["Expectancy", `${metrics.expectancy}`],
    ["Best / Worst Trade", `${metrics.best_trade} / ${metrics.worst_trade}`],
    ["Max Consec. Wins/Losses", `${metrics.max_consecutive_wins} / ${metrics.max_consecutive_losses}`],
    ["Recovery Factor", `${metrics.recovery_factor}`],
    ["Avg Holding (days)", `${metrics.avg_holding_period_days}`],
  ];
  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold text-text-primary">{title}</h3>
      <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 md:grid-cols-3">
        {rows.map(([label, value]) => (
          <div key={label} className="flex justify-between border-b border-border py-1 text-sm">
            <span className="text-text-secondary">{label}</span>
            <span className="font-financial font-medium text-text-primary">{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function BacktestingPage() {
  const { data: strategies } = useStrategies();
  const [strategy, setStrategy] = useState<StrategyOut | null>(null);
  const [instrumentQuery, setInstrumentQuery] = useState("");
  const [instrument, setInstrument] = useState<InstrumentOut | null>(null);
  const { data: instrumentResults } = useInstruments(instrumentQuery);

  const [initialCapital, setInitialCapital] = useState(100000);
  const [brokeragePct, setBrokeragePct] = useState(0.03);
  const [slippagePct, setSlippagePct] = useState(0.05);
  const [taxPct, setTaxPct] = useState(0);
  const [oosEnabled, setOosEnabled] = useState(false);
  const [oosSplit, setOosSplit] = useState(70);
  const [monteCarlo, setMonteCarlo] = useState(false);

  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const { data: job } = useBacktestJob(activeJobId);
  const completed = job?.status === "completed";
  const { data: result } = useBacktestResult(activeJobId, completed);
  const { data: trades } = useBacktestTrades(activeJobId, completed);

  const runMutation = useMutation({
    mutationFn: () =>
      apiFetch<BacktestJobOut>("/api/v1/backtests", {
        method: "POST",
        body: JSON.stringify({
          strategy_id: strategy!.id,
          instrument_id: instrument!.id,
          timeframe: "1d",
          initial_capital: initialCapital,
          brokerage_pct: brokeragePct,
          slippage_pct: slippagePct,
          tax_pct: taxPct,
          out_of_sample_split_pct: oosEnabled ? oosSplit : null,
          run_monte_carlo: monteCarlo,
        }),
      }),
    onSuccess: (data) => setActiveJobId(data.id),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Backtesting</h1>
        <p className="text-sm text-text-muted">
          Signal at bar close, fill at next bar&apos;s open -- the same rule/Python evaluators as the Strategy Builder, run bar-by-bar.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Configuration</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">Strategy</label>
              <Select
                value={strategy?.id ?? ""}
                onChange={(e) => setStrategy(strategies?.find((s) => s.id === e.target.value) ?? null)}
              >
                <option value="" disabled>
                  Select a strategy
                </option>
                {strategies?.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.code_type})
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">Instrument</label>
              <Input placeholder="Search..." value={instrumentQuery} onChange={(e) => setInstrumentQuery(e.target.value)} />
              {instrumentQuery && instrumentResults && (
                <div className="max-h-32 overflow-y-auto rounded-md border border-border">
                  {instrumentResults.map((i) => (
                    <button
                      key={i.id}
                      onClick={() => {
                        setInstrument(i);
                        setInstrumentQuery("");
                      }}
                      className="block w-full px-2 py-1.5 text-left text-sm text-text-secondary hover:bg-surface-elevated"
                    >
                      {i.symbol} ({i.exchange})
                    </button>
                  ))}
                </div>
              )}
              {instrument && <Badge tone="active">{instrument.symbol}</Badge>}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-text-secondary">Initial Capital</label>
              <Input type="number" value={initialCapital} onChange={(e) => setInitialCapital(Number(e.target.value))} />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-text-secondary">Brokerage %</label>
              <Input type="number" step="0.01" value={brokeragePct} onChange={(e) => setBrokeragePct(Number(e.target.value))} />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-text-secondary">Slippage %</label>
              <Input type="number" step="0.01" value={slippagePct} onChange={(e) => setSlippagePct(Number(e.target.value))} />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-text-secondary">Tax % (on profit)</label>
              <Input type="number" step="0.01" value={taxPct} onChange={(e) => setTaxPct(Number(e.target.value))} />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-6">
            <label className="flex items-center gap-2 text-sm text-text-secondary">
              <input type="checkbox" checked={oosEnabled} onChange={(e) => setOosEnabled(e.target.checked)} />
              Out-of-sample split
            </label>
            {oosEnabled && (
              <Input type="number" value={oosSplit} onChange={(e) => setOosSplit(Number(e.target.value))} className="w-24" />
            )}
            <label className="flex items-center gap-2 text-sm text-text-secondary">
              <input type="checkbox" checked={monteCarlo} onChange={(e) => setMonteCarlo(e.target.checked)} />
              Monte Carlo (trade resampling)
            </label>
          </div>

          <Button onClick={() => runMutation.mutate()} disabled={!strategy || !instrument || runMutation.isPending}>
            {runMutation.isPending ? "Starting..." : "Run Backtest"}
          </Button>

          {runMutation.error && (
            <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
              {runMutation.error instanceof ApiError ? runMutation.error.message : "Failed to start backtest"}
            </div>
          )}
        </CardContent>
      </Card>

      {job && (
        <Card>
          <CardHeader>
            <CardTitle>
              Status: <span className="capitalize">{job.status}</span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {job.status === "failed" && <p className="text-sm text-negative">{job.error_message}</p>}
            {(job.status === "pending" || job.status === "running") && (
              <p className="text-sm text-text-muted">Running bar-by-bar simulation...</p>
            )}

            {result && (
              <div className="space-y-6">
                <KpiGrid metrics={result.metrics} />

                <RiskAnalytics metrics={result.metrics} />

                <div>
                  <h3 className="mb-2 text-sm font-semibold text-text-primary">Equity Curve</h3>
                  <OscillatorChart
                    points={result.equity_curve.map(([ts, equity]) => ({ ts, value: equity }))}
                    bands={[initialCapital]}
                    color="#15803d"
                    height={220}
                  />
                </div>

                <MetricsGrid metrics={result.metrics} title={result.out_of_sample_metrics ? "Full Metrics (In-Sample)" : "Full Metrics"} />

                {result.out_of_sample_metrics && <MetricsGrid metrics={result.out_of_sample_metrics} title="Full Metrics (Out-of-Sample)" />}

                {result.monte_carlo && (
                  <div>
                    <h3 className="mb-2 text-sm font-semibold text-text-primary">Monte Carlo ({result.monte_carlo.simulations} runs, resampled trades)</h3>
                    <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 md:grid-cols-3 text-sm">
                      <div className="flex justify-between border-b border-border py-1">
                        <span className="text-text-secondary">Probability of Profit</span>
                        <span className="font-financial font-medium text-text-primary">{result.monte_carlo.probability_of_profit_pct}%</span>
                      </div>
                      <div className="flex justify-between border-b border-border py-1">
                        <span className="text-text-secondary">Final Equity (P5/P50/P95)</span>
                        <span className="font-financial font-medium text-text-primary">
                          {result.monte_carlo.final_equity_p5.toFixed(0)} / {result.monte_carlo.final_equity_p50.toFixed(0)} / {result.monte_carlo.final_equity_p95.toFixed(0)}
                        </span>
                      </div>
                      <div className="flex justify-between border-b border-border py-1">
                        <span className="text-text-secondary">Max Drawdown (P50/P95)</span>
                        <span className="font-financial font-medium text-text-primary">
                          {result.monte_carlo.max_drawdown_pct_p50}% / {result.monte_carlo.max_drawdown_pct_p95}%
                        </span>
                      </div>
                    </div>
                  </div>
                )}

                {trades && trades.length > 0 && (
                  <div>
                    <h3 className="mb-2 text-sm font-semibold text-text-primary">Trades ({trades.length})</h3>
                    <div className="max-h-96 overflow-y-auto">
                      <Table>
                        <Thead>
                          <tr>
                            <Th>Entry</Th>
                            <Th>Exit</Th>
                            <Th className="text-right">Qty</Th>
                            <Th className="text-right">PnL</Th>
                            <Th className="text-right">PnL %</Th>
                            <Th>Reason</Th>
                          </tr>
                        </Thead>
                        <Tbody>
                          {trades.map((t, i) => (
                            <tr key={i}>
                              <Td className="font-financial">{new Date(t.entry_ts).toLocaleDateString()} @ {t.entry_price.toFixed(2)}</Td>
                              <Td className="font-financial">{new Date(t.exit_ts).toLocaleDateString()} @ {t.exit_price.toFixed(2)}</Td>
                              <Td className="text-right font-financial">{t.quantity}</Td>
                              <Td className={`text-right font-financial ${t.pnl >= 0 ? "text-positive" : "text-negative"}`}>{t.pnl.toFixed(2)}</Td>
                              <Td className={`text-right font-financial ${t.pnl_pct >= 0 ? "text-positive" : "text-negative"}`}>{t.pnl_pct.toFixed(2)}%</Td>
                              <Td className="text-text-muted">{t.exit_reason.replace("_", " ")}</Td>
                            </tr>
                          ))}
                        </Tbody>
                      </Table>
                    </div>
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
