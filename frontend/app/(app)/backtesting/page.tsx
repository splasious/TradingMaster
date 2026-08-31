"use client";

import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { OscillatorChart } from "@/components/charts/oscillator-chart";
import { InstrumentMultiSelect } from "@/components/market-data/instrument-multiselect";
import { WatchlistLoader } from "@/components/market-data/watchlist-loader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import { useBacktestJob, useBacktestResult, useBacktestTrades, useStrategies } from "@/lib/hooks";
import {
  TIMEFRAMES,
  type BacktestJobOut,
  type BacktestMetrics,
  type BacktestTradeOut,
  type InstrumentOut,
  type StrategyOut,
} from "@/lib/types";

interface QueuedBacktest {
  instrument: InstrumentOut;
  jobId: string | null;
  error: string | null;
}

interface TaggedTrade extends BacktestTradeOut {
  symbol: string;
}

interface PerInstrumentResult {
  symbol: string;
  metrics: BacktestMetrics;
  trades: TaggedTrade[];
}

/** Fetches one queued job's result+trades and reports them up once
 * complete -- renders nothing; exists purely so each job in the list can
 * use its own hooks (React forbids calling hooks in a variable-length
 * loop directly) while still feeding one combined-results accumulator. */
function ResultCollector({ queued, onLoaded }: { queued: QueuedBacktest; onLoaded: (r: PerInstrumentResult) => void }) {
  const { data: job } = useBacktestJob(queued.jobId);
  const completed = job?.status === "completed";
  const { data: result } = useBacktestResult(queued.jobId, completed);
  const { data: trades } = useBacktestTrades(queued.jobId, completed);

  useEffect(() => {
    if (completed && result && trades) {
      onLoaded({
        symbol: queued.instrument.symbol,
        metrics: result.metrics,
        trades: trades.map((t) => ({ ...t, symbol: queued.instrument.symbol })),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [completed, result, trades]);

  return null;
}

function BacktestJobRow({ queued, isFocused, onSelect }: { queued: QueuedBacktest; isFocused: boolean; onSelect: () => void }) {
  const { data: job } = useBacktestJob(queued.jobId);
  const status = queued.error ? "failed" : (job?.status ?? "pending");

  return (
    <button
      onClick={onSelect}
      className={`flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm ${
        isFocused ? "bg-active-soft text-active" : "text-text-secondary hover:bg-surface-elevated"
      }`}
    >
      <span className="font-medium">{queued.instrument.symbol}</span>
      <span className="flex items-center gap-1.5 text-xs">
        {status === "running" || status === "pending" ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-active" />
        ) : status === "completed" ? (
          <CheckCircle2 className="h-3.5 w-3.5 text-positive" />
        ) : (
          <XCircle className="h-3.5 w-3.5 text-negative" />
        )}
        <span className="capitalize">{status}</span>
      </span>
    </button>
  );
}

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

function TradesTable({ trades, showSymbol }: { trades: TaggedTrade[]; showSymbol: boolean }) {
  return (
    <div className="max-h-96 overflow-y-auto">
      <Table>
        <Thead>
          <tr>
            {showSymbol && <Th>Symbol</Th>}
            <Th>Entry</Th>
            <Th>Exit</Th>
            <Th className="text-right">Qty</Th>
            <Th className="text-right">Allocated</Th>
            <Th className="text-right">PnL</Th>
            <Th className="text-right">PnL %</Th>
            <Th>Reason</Th>
          </tr>
        </Thead>
        <Tbody>
          {trades.map((t, i) => (
            <tr key={i}>
              {showSymbol && <Td className="font-medium">{t.symbol}</Td>}
              <Td className="font-financial">{new Date(t.entry_ts).toLocaleDateString()} @ {t.entry_price.toFixed(2)}</Td>
              <Td className="font-financial">{new Date(t.exit_ts).toLocaleDateString()} @ {t.exit_price.toFixed(2)}</Td>
              <Td className="text-right font-financial">{Math.round(t.quantity)}</Td>
              <Td className="text-right font-financial">{(t.quantity * t.entry_price).toLocaleString(undefined, { maximumFractionDigits: 0 })}</Td>
              <Td className={`text-right font-financial ${t.pnl >= 0 ? "text-positive" : "text-negative"}`}>{t.pnl.toFixed(2)}</Td>
              <Td className={`text-right font-financial ${t.pnl_pct >= 0 ? "text-positive" : "text-negative"}`}>{t.pnl_pct.toFixed(2)}%</Td>
              <Td className="text-text-muted">{t.exit_reason.replace("_", " ")}</Td>
            </tr>
          ))}
        </Tbody>
      </Table>
    </div>
  );
}

export default function BacktestingPage() {
  const { data: strategies } = useStrategies();
  const [strategy, setStrategy] = useState<StrategyOut | null>(null);
  const [instruments, setInstruments] = useState<InstrumentOut[]>([]);

  const [timeframe, setTimeframe] = useState("1d");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [initialCapital, setInitialCapital] = useState(100000);
  const [sizingOverride, setSizingOverride] = useState(false);
  const [sizingType, setSizingType] = useState<"fixed_quantity" | "percent_capital">("fixed_quantity");
  const [sizingValue, setSizingValue] = useState(1);
  const [brokeragePct, setBrokeragePct] = useState(0.03);
  const [slippagePct, setSlippagePct] = useState(0.05);
  const [taxPct, setTaxPct] = useState(0);
  const [oosEnabled, setOosEnabled] = useState(false);
  const [oosSplit, setOosSplit] = useState(70);
  const [monteCarlo, setMonteCarlo] = useState(false);

  const [queuedJobs, setQueuedJobs] = useState<QueuedBacktest[]>([]);
  const [focusedJobId, setFocusedJobId] = useState<string | null>(null);
  const [perInstrumentResults, setPerInstrumentResults] = useState<Map<string, PerInstrumentResult>>(new Map());

  const { data: job } = useBacktestJob(focusedJobId);
  const completed = job?.status === "completed";
  const { data: result } = useBacktestResult(focusedJobId, completed);
  const { data: trades } = useBacktestTrades(focusedJobId, completed);
  const focusedSymbol = queuedJobs.find((q) => q.jobId === focusedJobId)?.instrument.symbol;
  const taggedTrades = useMemo<TaggedTrade[]>(
    () => (trades ?? []).map((t) => ({ ...t, symbol: focusedSymbol ?? "" })),
    [trades, focusedSymbol],
  );

  const combined = useMemo(() => {
    const results = [...perInstrumentResults.values()];
    if (results.length < 2) return null;
    const allTrades = results.flatMap((r) => r.trades).sort((a, b) => new Date(a.entry_ts).getTime() - new Date(b.entry_ts).getTime());
    const wins = allTrades.filter((t) => t.pnl > 0);
    const losses = allTrades.filter((t) => t.pnl <= 0);
    const netProfit = allTrades.reduce((sum, t) => sum + t.pnl, 0);
    const grossProfit = wins.reduce((sum, t) => sum + t.pnl, 0);
    const grossLoss = Math.abs(losses.reduce((sum, t) => sum + t.pnl, 0));
    return {
      instrumentCount: results.length,
      netProfit,
      totalTrades: allTrades.length,
      winRatePct: allTrades.length ? (wins.length / allTrades.length) * 100 : 0,
      profitFactor: grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : 0,
      totalAllocated: allTrades.reduce((sum, t) => sum + t.quantity * t.entry_price, 0),
      trades: allTrades,
    };
  }, [perInstrumentResults]);

  const runMutation = useMutation({
    mutationFn: async () => {
      const settled = await Promise.allSettled(
        instruments.map((instrument) =>
          apiFetch<BacktestJobOut>("/api/v1/backtests", {
            method: "POST",
            body: JSON.stringify({
              strategy_id: strategy!.id,
              instrument_id: instrument.id,
              timeframe,
              start_date: startDate || null,
              end_date: endDate || null,
              initial_capital: initialCapital,
              position_sizing_type: sizingOverride ? sizingType : null,
              position_sizing_value: sizingOverride ? sizingValue : null,
              brokerage_pct: brokeragePct,
              slippage_pct: slippagePct,
              tax_pct: taxPct,
              out_of_sample_split_pct: oosEnabled ? oosSplit : null,
              run_monte_carlo: monteCarlo,
            }),
          }).then((job) => ({ instrument, job })),
        ),
      );
      return settled.map((outcome, i): QueuedBacktest =>
        outcome.status === "fulfilled"
          ? { instrument: outcome.value.instrument, jobId: outcome.value.job.id, error: null }
          : { instrument: instruments[i], jobId: null, error: outcome.reason instanceof ApiError ? outcome.reason.message : "Failed to start" },
      );
    },
    onSuccess: (results) => {
      setQueuedJobs(results);
      // Auto-focus only for a single-instrument run -- with multiple
      // instruments, Combined Results is the default view; a per-instrument
      // drill-down only appears once the user clicks a row in Queued Backtests.
      setFocusedJobId(results.length === 1 ? (results[0].jobId ?? null) : null);
      setPerInstrumentResults(new Map());
    },
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
              <label className="text-sm font-medium text-text-secondary">Instruments</label>
              <WatchlistLoader onLoad={(loaded) => setInstruments((prev) => {
                const merged = new Map(prev.map((i) => [i.id, i] as const));
                for (const i of loaded) merged.set(i.id, i);
                return [...merged.values()];
              })} />
              <InstrumentMultiSelect value={instruments} onChange={setInstruments} />
              {instruments.length > 0 && (
                <p className="text-xs text-text-muted">
                  {instruments.length} instrument{instruments.length === 1 ? "" : "s"} selected -- one backtest job runs per instrument.
                </p>
              )}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-text-secondary">Timeframe</label>
              <Select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
                {TIMEFRAMES.map((tf) => (
                  <option key={tf} value={tf}>{tf}</option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-text-secondary">Start Date</label>
              <Input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-text-secondary">End Date</label>
              <Input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
            </div>
            <div className="flex items-end">
              <p className="text-xs text-text-muted">Leave dates blank to use all available history.</p>
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

          <div className="space-y-2">
            <label className="flex items-center gap-2 text-sm text-text-secondary">
              <input type="checkbox" checked={sizingOverride} onChange={(e) => setSizingOverride(e.target.checked)} />
              Override position sizing for this run (default: use the strategy&apos;s own sizing)
            </label>
            {sizingOverride && (
              <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-text-secondary">Sizing Basis</label>
                  <Select value={sizingType} onChange={(e) => setSizingType(e.target.value as typeof sizingType)}>
                    <option value="fixed_quantity">Capital per Share (fixed qty)</option>
                    <option value="percent_capital">% of Capital</option>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-text-secondary">
                    {sizingType === "fixed_quantity" ? "Shares per Trade" : "% of Capital"}
                  </label>
                  <Input type="number" value={sizingValue} onChange={(e) => setSizingValue(Number(e.target.value))} />
                </div>
              </div>
            )}
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

          <Button onClick={() => runMutation.mutate()} disabled={!strategy || !instruments.length || runMutation.isPending}>
            {runMutation.isPending ? "Starting..." : instruments.length > 1 ? `Run ${instruments.length} Backtests` : "Run Backtest"}
          </Button>

          {queuedJobs.some((q) => q.error) && (
            <div className="space-y-1 rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
              {queuedJobs.filter((q) => q.error).map((q) => (
                <div key={q.instrument.id}>{q.instrument.symbol}: {q.error}</div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {queuedJobs.length > 1 &&
        queuedJobs.map(
          (q) =>
            q.jobId && (
              <ResultCollector
                key={q.instrument.id}
                queued={q}
                onLoaded={(r) =>
                  setPerInstrumentResults((prev) => {
                    const next = new Map(prev);
                    next.set(q.instrument.id, r);
                    return next;
                  })
                }
              />
            ),
        )}

      {queuedJobs.length > 1 && (
        <Card>
          <CardHeader>
            <CardTitle>Queued Backtests ({queuedJobs.length})</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1">
            {queuedJobs.map((q) => (
              <BacktestJobRow
                key={q.instrument.id}
                queued={q}
                isFocused={q.jobId === focusedJobId}
                onSelect={() => q.jobId && setFocusedJobId(q.jobId)}
              />
            ))}
          </CardContent>
        </Card>
      )}

      {queuedJobs.length > 1 && (
        <Card>
          <CardHeader>
            <CardTitle>
              Combined Results {combined ? `(${combined.instrumentCount} of ${queuedJobs.length} instruments completed)` : ""}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {!combined ? (
              <p className="text-sm text-text-muted">Waiting for at least 2 instruments to complete...</p>
            ) : (
              <div className="space-y-6">
                <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                  <KpiTile
                    label="Combined Net Profit"
                    value={combined.netProfit.toLocaleString(undefined, { maximumFractionDigits: 0, signDisplay: "always" })}
                    tone={combined.netProfit >= 0 ? "positive" : "negative"}
                  />
                  <KpiTile label="Combined Win Rate" value={`${combined.winRatePct.toFixed(1)}%`} />
                  <KpiTile
                    label="Combined Profit Factor"
                    value={Number.isFinite(combined.profitFactor) ? combined.profitFactor.toFixed(2) : "∞"}
                    tone={combined.profitFactor >= 1 ? "positive" : "negative"}
                  />
                  <KpiTile label="Total Trades" value={`${combined.totalTrades}`} />
                </div>
                <p className="text-xs text-text-muted">
                  Total capital allocated across all trades:{" "}
                  <span className="font-financial font-medium text-text-primary">
                    {combined.totalAllocated.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                  </span>
                </p>
                <div>
                  <h3 className="mb-2 text-sm font-semibold text-text-primary">All Trades ({combined.trades.length})</h3>
                  <TradesTable trades={combined.trades} showSymbol />
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {job && (
        <Card>
          <CardHeader>
            <CardTitle>
              {queuedJobs.length > 1 && `${queuedJobs.find((q) => q.jobId === focusedJobId)?.instrument.symbol} -- `}
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

                {taggedTrades.length > 0 && (
                  <div>
                    <h3 className="mb-2 text-sm font-semibold text-text-primary">Trades ({taggedTrades.length})</h3>
                    <TradesTable trades={taggedTrades} showSymbol={queuedJobs.length > 1} />
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
