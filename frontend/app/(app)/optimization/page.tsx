"use client";

import { useMutation } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import { useInstruments, useOptimizationJob, useOptimizationResult, useStrategies } from "@/lib/hooks";
import type { InstrumentOut, OptimizationJobOut, ParamRangeIn, StrategyOut } from "@/lib/types";

const RANK_METRICS = ["net_profit", "sharpe_ratio", "profit_factor", "cagr_pct", "win_rate_pct"];

export default function OptimizationPage() {
  const { data: strategies } = useStrategies();
  const pythonStrategies = strategies?.filter((s) => s.code_type === "python");
  const [strategy, setStrategy] = useState<StrategyOut | null>(null);

  const [instrumentQuery, setInstrumentQuery] = useState("");
  const [instrument, setInstrument] = useState<InstrumentOut | null>(null);
  const { data: instrumentResults } = useInstruments(instrumentQuery);

  const [ranges, setRanges] = useState<ParamRangeIn[]>([{ name: "threshold", min: 0, max: 10, step: 5 }]);
  const [rankMetric, setRankMetric] = useState("sharpe_ratio");

  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const { data: job } = useOptimizationJob(activeJobId);
  const { data: result } = useOptimizationResult(activeJobId, job?.status === "completed");

  const runMutation = useMutation({
    mutationFn: () =>
      apiFetch<OptimizationJobOut>("/api/v1/optimization", {
        method: "POST",
        body: JSON.stringify({
          strategy_id: strategy!.id,
          instrument_id: instrument!.id,
          timeframe: "1d",
          param_ranges: ranges,
          rank_metric: rankMetric,
        }),
      }),
    onSuccess: (data) => setActiveJobId(data.id),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Strategy Optimization</h1>
        <p className="text-sm text-text-muted">
          Grid search over a Python strategy&apos;s <code>params</code> -- re-runs the same backtest engine for every combination.
        </p>
      </div>

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

          <Button onClick={() => runMutation.mutate()} disabled={!strategy || !instrument || runMutation.isPending}>
            {runMutation.isPending ? "Starting..." : "Run Optimization"}
          </Button>

          {runMutation.error && (
            <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
              {runMutation.error instanceof ApiError ? runMutation.error.message : "Failed to start optimization"}
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
    </div>
  );
}
