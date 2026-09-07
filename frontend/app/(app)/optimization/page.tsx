"use client";

import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, Loader2, Plus, Trash2, XCircle } from "lucide-react";
import { useState } from "react";

import { InstrumentMultiSelect } from "@/components/market-data/instrument-multiselect";
import { WatchlistLoader } from "@/components/market-data/watchlist-loader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import {
  useOptimizationJob,
  useOptimizationResult,
  usePortfolioOptimizationJob,
  usePortfolioOptimizationResult,
  useStrategies,
} from "@/lib/hooks";
import type {
  InstrumentOut,
  OptimizationJobOut,
  ParamRangeIn,
  PortfolioOptimizationJobOut,
  StrategyOut,
} from "@/lib/types";

const RANK_METRICS = ["net_profit", "sharpe_ratio", "profit_factor", "cagr_pct", "win_rate_pct"];

interface QueuedOptimization {
  instrument: InstrumentOut;
  jobId: string | null;
  error: string | null;
}

function OptimizationJobRow({ queued, isFocused, onSelect }: { queued: QueuedOptimization; isFocused: boolean; onSelect: () => void }) {
  const { data: job } = useOptimizationJob(queued.jobId);
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

export default function OptimizationPage() {
  const { data: strategies } = useStrategies();
  const pythonStrategies = strategies?.filter((s) => s.code_type === "python");
  const [strategy, setStrategy] = useState<StrategyOut | null>(null);

  // "single" mirrors the historical behavior (one independent grid search
  // per instrument). "portfolio" evaluates every parameter combination as
  // one real portfolio backtest across the whole basket at once (shared
  // capital, position-score gating) -- see portfolio_optimization_runner.py.
  const [mode, setMode] = useState<"single" | "portfolio">("single");

  const [instruments, setInstruments] = useState<InstrumentOut[]>([]);

  const [ranges, setRanges] = useState<ParamRangeIn[]>([{ name: "threshold", min: 0, max: 10, step: 5 }]);
  const [rankMetric, setRankMetric] = useState("sharpe_ratio");

  const [positionSizePct, setPositionSizePct] = useState(10);
  const [maxOpenPositions, setMaxOpenPositions] = useState(10);

  const [queuedJobs, setQueuedJobs] = useState<QueuedOptimization[]>([]);
  const [focusedJobId, setFocusedJobId] = useState<string | null>(null);
  const { data: job } = useOptimizationJob(focusedJobId);
  const { data: result } = useOptimizationResult(focusedJobId, job?.status === "completed");

  const [portfolioJobId, setPortfolioJobId] = useState<string | null>(null);
  const { data: portfolioJob } = usePortfolioOptimizationJob(portfolioJobId);
  const { data: portfolioResult } = usePortfolioOptimizationResult(portfolioJobId, portfolioJob?.status === "completed");

  const runMutation = useMutation({
    mutationFn: async () => {
      const settled = await Promise.allSettled(
        instruments.map((instrument) =>
          apiFetch<OptimizationJobOut>("/api/v1/optimization", {
            method: "POST",
            body: JSON.stringify({
              strategy_id: strategy!.id,
              instrument_id: instrument.id,
              timeframe: "1d",
              param_ranges: ranges,
              rank_metric: rankMetric,
            }),
          }).then((job) => ({ instrument, job })),
        ),
      );
      return settled.map((outcome, i): QueuedOptimization =>
        outcome.status === "fulfilled"
          ? { instrument: outcome.value.instrument, jobId: outcome.value.job.id, error: null }
          : { instrument: instruments[i], jobId: null, error: outcome.reason instanceof ApiError ? outcome.reason.message : "Failed to start" },
      );
    },
    onSuccess: (results) => {
      setQueuedJobs(results);
      setFocusedJobId(results.find((r) => r.jobId)?.jobId ?? null);
    },
  });

  const portfolioRunMutation = useMutation({
    mutationFn: () =>
      apiFetch<PortfolioOptimizationJobOut>("/api/v1/portfolio-optimization", {
        method: "POST",
        body: JSON.stringify({
          strategy_id: strategy!.id,
          instrument_ids: instruments.map((i) => i.id),
          timeframe: "1d",
          position_size_pct: positionSizePct,
          max_open_positions: maxOpenPositions,
          param_ranges: ranges,
          rank_metric: rankMetric,
        }),
      }),
    onSuccess: (job) => setPortfolioJobId(job.id),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Strategy Optimization</h1>
        <p className="text-sm text-text-muted">
          Grid search over a Python strategy&apos;s <code>params</code> -- re-runs the same backtest engine for every combination.
        </p>
      </div>

      <div className="flex gap-2 rounded-lg border border-border bg-surface-elevated p-1 w-fit">
        <button
          onClick={() => setMode("single")}
          className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
            mode === "single" ? "bg-active-soft text-active" : "text-text-secondary hover:text-text-primary"
          }`}
        >
          Per-Instrument
        </button>
        <button
          onClick={() => setMode("portfolio")}
          className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
            mode === "portfolio" ? "bg-active-soft text-active" : "text-text-secondary hover:text-text-primary"
          }`}
        >
          Portfolio
        </button>
      </div>
      {mode === "portfolio" && (
        <p className="-mt-4 text-xs text-text-muted">
          Every parameter combination is evaluated as one real portfolio backtest across the whole basket (shared
          capital pool, position-score gating when signals exceed open slots) -- not one instrument in isolation with
          unlimited capital. A combination that looks best per-instrument can perform far worse once actually run as
          a portfolio, since it can&apos;t see which instruments compete for scarce capital/slots.
        </p>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Configuration</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">Python Strategy</label>
              <Select value={strategy?.id ?? ""} onChange={(e) => setStrategy(pythonStrategies?.find((s) => s.id === e.target.value) ?? null)}>
                <option value="" disabled>
                  Select a Python strategy
                </option>
                {pythonStrategies?.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </Select>
              {!pythonStrategies?.length && <p className="text-xs text-text-muted">No Python strategies yet -- optimization needs named params.</p>}
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
                  {mode === "single"
                    ? `${instruments.length} instrument${instruments.length === 1 ? "" : "s"} selected -- one grid search runs per instrument.`
                    : `${instruments.length} instrument${instruments.length === 1 ? "" : "s"} selected -- one portfolio grid search runs across all of them together${instruments.length < 2 ? " (needs at least 2)" : ""}.`}
                </p>
              )}
            </div>
          </div>

          {mode === "portfolio" && (
            <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-text-secondary">Position Size (% of equity)</label>
                <Input type="number" step="1" value={positionSizePct} onChange={(e) => setPositionSizePct(Number(e.target.value))} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-text-secondary">Max Open Positions</label>
                <Input type="number" step="1" value={maxOpenPositions} onChange={(e) => setMaxOpenPositions(Number(e.target.value))} />
              </div>
            </div>
          )}

          <div className="space-y-2">
            <label className="text-xs font-medium uppercase tracking-wide text-text-muted">Parameter Ranges</label>
            {ranges.map((r, i) => (
              <div key={i} className="flex items-center gap-2">
                <Input
                  placeholder="param name"
                  value={r.name}
                  onChange={(e) => setRanges(ranges.map((x, idx) => (idx === i ? { ...x, name: e.target.value } : x)))}
                  className="w-32"
                />
                <Input type="number" placeholder="min" value={r.min} onChange={(e) => setRanges(ranges.map((x, idx) => (idx === i ? { ...x, min: Number(e.target.value) } : x)))} className="w-24" />
                <span className="text-text-muted">to</span>
                <Input type="number" placeholder="max" value={r.max} onChange={(e) => setRanges(ranges.map((x, idx) => (idx === i ? { ...x, max: Number(e.target.value) } : x)))} className="w-24" />
                <span className="text-text-muted">step</span>
                <Input type="number" placeholder="step" value={r.step} onChange={(e) => setRanges(ranges.map((x, idx) => (idx === i ? { ...x, step: Number(e.target.value) } : x)))} className="w-24" />
                <Button variant="ghost" size="sm" onClick={() => setRanges(ranges.filter((_, idx) => idx !== i))} disabled={ranges.length === 1}>
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            ))}
            <Button variant="secondary" size="sm" onClick={() => setRanges([...ranges, { name: "", min: 0, max: 10, step: 1 }])}>
              <Plus className="h-3.5 w-3.5" /> Add parameter
            </Button>
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium text-text-secondary">Rank by</label>
            <Select value={rankMetric} onChange={(e) => setRankMetric(e.target.value)} className="w-48">
              {RANK_METRICS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </Select>
          </div>

          {mode === "single" ? (
            <>
              <Button onClick={() => runMutation.mutate()} disabled={!strategy || !instruments.length || runMutation.isPending}>
                {runMutation.isPending ? "Starting..." : instruments.length > 1 ? `Run ${instruments.length} Optimizations` : "Run Optimization"}
              </Button>

              {queuedJobs.some((q) => q.error) && (
                <div className="space-y-1 rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
                  {queuedJobs.filter((q) => q.error).map((q) => (
                    <div key={q.instrument.id}>{q.instrument.symbol}: {q.error}</div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <>
              <Button
                onClick={() => portfolioRunMutation.mutate()}
                disabled={!strategy || instruments.length < 2 || portfolioRunMutation.isPending}
              >
                {portfolioRunMutation.isPending ? "Starting..." : `Run Portfolio Optimization (${instruments.length} instruments)`}
              </Button>

              {portfolioRunMutation.isError && (
                <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
                  {portfolioRunMutation.error instanceof ApiError ? portfolioRunMutation.error.message : "Failed to start"}
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

      {mode === "single" && queuedJobs.length > 1 && (
        <Card>
          <CardHeader>
            <CardTitle>Queued Optimizations ({queuedJobs.length})</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1">
            {queuedJobs.map((q) => (
              <OptimizationJobRow
                key={q.instrument.id}
                queued={q}
                isFocused={q.jobId === focusedJobId}
                onSelect={() => q.jobId && setFocusedJobId(q.jobId)}
              />
            ))}
          </CardContent>
        </Card>
      )}

      {mode === "single" && job && (
        <Card>
          <CardHeader>
            <CardTitle>
              {queuedJobs.length > 1 && `${queuedJobs.find((q) => q.jobId === focusedJobId)?.instrument.symbol} -- `}
              Status: <span className="capitalize">{job.status}</span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {job.status === "failed" && <p className="text-sm text-negative">{job.error_message}</p>}
            {(job.status === "pending" || job.status === "running") && <p className="text-sm text-text-muted">Running grid search...</p>}

            {result && (
              <div className="space-y-2">
                <p className="text-xs text-text-muted">
                  {result.runs.length} combinations, ranked by <span className="font-medium">{rankMetric}</span>. Overfitting warning: the best
                  in-sample parameters are not guaranteed to hold out-of-sample -- validate with a backtest&apos;s out-of-sample split before trusting a result.
                </p>
                <Table>
                  <Thead>
                    <tr>
                      {Object.keys(result.runs[0]?.params ?? {}).map((p) => (
                        <Th key={p}>{p}</Th>
                      ))}
                      <Th className="text-right">Net Profit</Th>
                      <Th className="text-right">Sharpe</Th>
                      <Th className="text-right">Profit Factor</Th>
                      <Th className="text-right">Win Rate</Th>
                      <Th className="text-right">Trades</Th>
                    </tr>
                  </Thead>
                  <Tbody>
                    {result.runs.map((run, i) => (
                      <tr key={i} className={i === 0 ? "bg-positive-soft" : undefined}>
                        {Object.values(run.params).map((v, j) => (
                          <Td key={j} className="font-financial">{v}</Td>
                        ))}
                        <Td className="text-right font-financial">{run.metrics.net_profit}</Td>
                        <Td className="text-right font-financial">{run.metrics.sharpe_ratio}</Td>
                        <Td className="text-right font-financial">{run.metrics.profit_factor}</Td>
                        <Td className="text-right font-financial">{run.metrics.win_rate_pct}%</Td>
                        <Td className="text-right font-financial">{run.metrics.num_trades}</Td>
                      </tr>
                    ))}
                  </Tbody>
                </Table>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {mode === "portfolio" && portfolioJob && (
        <Card>
          <CardHeader>
            <CardTitle>
              Portfolio Status: <span className="capitalize">{portfolioJob.status}</span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {portfolioJob.status === "failed" && <p className="text-sm text-negative">{portfolioJob.error_message}</p>}
            {(portfolioJob.status === "pending" || portfolioJob.status === "running") && (
              <p className="text-sm text-text-muted">Running portfolio grid search across {instruments.length} instruments...</p>
            )}

            {portfolioResult && (
              <div className="space-y-2">
                <p className="text-xs text-text-muted">
                  {portfolioResult.runs.length} combinations, each a full portfolio backtest across the basket, ranked by{" "}
                  <span className="font-medium">{rankMetric}</span>. Overfitting warning: the best in-sample parameters are not
                  guaranteed to hold out-of-sample -- validate with a fresh portfolio backtest on more recent data before trusting a result.
                </p>
                <Table>
                  <Thead>
                    <tr>
                      {Object.keys(portfolioResult.runs[0]?.params ?? {}).map((p) => (
                        <Th key={p}>{p}</Th>
                      ))}
                      <Th className="text-right">Net Profit</Th>
                      <Th className="text-right">Sharpe</Th>
                      <Th className="text-right">Profit Factor</Th>
                      <Th className="text-right">Win Rate</Th>
                      <Th className="text-right">Max DD</Th>
                      <Th className="text-right">Trades</Th>
                      <Th className="text-right">Instruments</Th>
                    </tr>
                  </Thead>
                  <Tbody>
                    {portfolioResult.runs.map((run, i) => (
                      <tr key={i} className={i === 0 ? "bg-positive-soft" : undefined}>
                        {Object.values(run.params).map((v, j) => (
                          <Td key={j} className="font-financial">{v}</Td>
                        ))}
                        <Td className="text-right font-financial">{run.metrics.net_profit}</Td>
                        <Td className="text-right font-financial">{run.metrics.sharpe_ratio}</Td>
                        <Td className="text-right font-financial">{run.metrics.profit_factor}</Td>
                        <Td className="text-right font-financial">{run.metrics.win_rate_pct}%</Td>
                        <Td className="text-right font-financial">{run.metrics.max_drawdown_pct}%</Td>
                        <Td className="text-right font-financial">{run.metrics.num_trades}</Td>
                        <Td className="text-right font-financial">
                          {run.instrument_count}
                          {run.skipped_symbols.length > 0 && (
                            <span className="text-text-muted"> ({run.skipped_symbols.length} skipped)</span>
                          )}
                        </Td>
                      </tr>
                    ))}
                  </Tbody>
                </Table>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
