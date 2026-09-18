"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import { FireOrderModal } from "@/components/trading/fire-order-modal";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import { useIndicatorList, useStrategies } from "@/lib/hooks";
import { marketLabel } from "@/lib/market";
import { TIMEFRAMES } from "@/lib/types";
import type { InstrumentOut, ScanCondition, ScanOperator, ScanResponse, SavedScanOut, StrategyScanResponse } from "@/lib/types";

const RAW_FIELDS = ["open", "high", "low", "close", "volume"];
const OPERATORS: ScanOperator[] = [">", "<", ">=", "<=", "=="];

function useFieldOptions() {
  const { data: indicators } = useIndicatorList();
  return useMemo(() => {
    const options = RAW_FIELDS.map((f) => ({ value: f, label: f }));
    for (const spec of indicators ?? []) {
      for (const output of spec.output_fields) {
        options.push({ value: `${spec.code}.${output}`, label: `${spec.name} (${output})` });
      }
    }
    return options;
  }, [indicators]);
}

function signalTone(signal: string): "positive" | "negative" | "neutral" {
  if (signal === "BUY" || signal === "COVER") return "positive";
  if (signal === "SELL" || signal === "SHORT") return "negative";
  return "neutral";
}

export default function ScannerPage() {
  const fieldOptions = useFieldOptions();
  const queryClient = useQueryClient();

  const [mode, setMode] = useState<"fields" | "strategy">("fields");
  const [fireOrderInstrument, setFireOrderInstrument] = useState<InstrumentOut | null>(null);

  const [exchange, setExchange] = useState("");
  const [timeframe, setTimeframe] = useState("1d");
  const [conditions, setConditions] = useState<ScanCondition[]>([{ field: "rsi.rsi", operator: ">", value: 70 }]);
  const [scanName, setScanName] = useState("");
  const [result, setResult] = useState<ScanResponse | null>(null);

  const { data: savedScans } = useQuery({
    queryKey: ["saved-scans"],
    queryFn: () => apiFetch<SavedScanOut[]>("/api/v1/scanner/saved"),
  });

  const runMutation = useMutation({
    mutationFn: () =>
      apiFetch<ScanResponse>("/api/v1/scanner/run", {
        method: "POST",
        body: JSON.stringify({ exchange: exchange || null, timeframe, conditions }),
      }),
    onSuccess: setResult,
  });

  const saveMutation = useMutation({
    mutationFn: () =>
      apiFetch<SavedScanOut>("/api/v1/scanner/saved", {
        method: "POST",
        body: JSON.stringify({ name: scanName, exchange: exchange || null, timeframe, conditions }),
      }),
    onSuccess: () => {
      setScanName("");
      queryClient.invalidateQueries({ queryKey: ["saved-scans"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/scanner/saved/${id}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["saved-scans"] }),
  });

  // --- Python Strategy scan mode ---
  const { data: strategies } = useStrategies();
  const pythonStrategies = useMemo(() => (strategies ?? []).filter((s) => s.code_type === "python"), [strategies]);
  const [strategyId, setStrategyId] = useState("");
  const [strategyExchange, setStrategyExchange] = useState("");
  const [strategyTimeframe, setStrategyTimeframe] = useState("1d");
  const [strategyResult, setStrategyResult] = useState<StrategyScanResponse | null>(null);

  function selectStrategy(id: string) {
    setStrategyId(id);
    const picked = pythonStrategies.find((s) => s.id === id);
    setStrategyTimeframe(picked?.latest_version?.timeframe ?? "1d");
  }

  const runStrategyMutation = useMutation({
    mutationFn: () =>
      apiFetch<StrategyScanResponse>("/api/v1/scanner/run-strategy", {
        method: "POST",
        body: JSON.stringify({ strategy_id: strategyId, exchange: strategyExchange || null, timeframe: strategyTimeframe }),
      }),
    onSuccess: setStrategyResult,
  });

  function updateCondition(index: number, patch: Partial<ScanCondition>) {
    setConditions((prev) => prev.map((c, i) => (i === index ? { ...c, ...patch } : c)));
  }

  function loadSavedScan(scan: SavedScanOut) {
    setExchange(scan.exchange ?? "");
    setTimeframe(scan.timeframe);
    setConditions(scan.conditions);
    setResult(null);
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Market Scanner</h1>
          <p className="text-sm text-text-muted">
            {mode === "fields"
              ? "Filter the instrument catalog by price, volume, or indicator values."
              : "Run one of your Python strategies against the market and see who it's saying BUY/SELL/SHORT on right now."}
          </p>
        </div>
        <div className="flex gap-1 rounded-md border border-border bg-surface-elevated p-1">
          <button
            onClick={() => setMode("fields")}
            className={`rounded px-3 py-1.5 text-sm font-medium transition-colors ${
              mode === "fields" ? "bg-brand text-white" : "text-text-secondary hover:text-text-primary"
            }`}
          >
            Field Conditions
          </button>
          <button
            onClick={() => setMode("strategy")}
            className={`rounded px-3 py-1.5 text-sm font-medium transition-colors ${
              mode === "strategy" ? "bg-brand text-white" : "text-text-secondary hover:text-text-primary"
            }`}
          >
            Python Strategy
          </button>
        </div>
      </div>

      {mode === "fields" && (
        <>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader>
                <CardTitle>Filter Conditions</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="flex gap-3">
                  <div className="space-y-1.5">
                    <label className="text-xs font-medium text-text-secondary">Exchange</label>
                    <Select value={exchange} onChange={(e) => setExchange(e.target.value)} className="w-36">
                      <option value="">All Markets</option>
                      <option value="NSE">NSE Markets</option>
                      <option value="DELTA">Delta Markets</option>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs font-medium text-text-secondary">Timeframe</label>
                    <Select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="w-24">
                      {TIMEFRAMES.map((tf) => (
                        <option key={tf} value={tf}>
                          {tf}
                        </option>
                      ))}
                    </Select>
                  </div>
                </div>

                <div className="space-y-2">
                  {conditions.map((condition, i) => (
                    <div key={i} className="flex items-center gap-2">
                      <Select
                        value={condition.field}
                        onChange={(e) => updateCondition(i, { field: e.target.value })}
                        className="flex-1"
                      >
                        {fieldOptions.map((opt) => (
                          <option key={opt.value} value={opt.value}>
                            {opt.label}
                          </option>
                        ))}
                      </Select>
                      <Select
                        value={condition.operator}
                        onChange={(e) => updateCondition(i, { operator: e.target.value as ScanOperator })}
                        className="w-20"
                      >
                        {OPERATORS.map((op) => (
                          <option key={op} value={op}>
                            {op}
                          </option>
                        ))}
                      </Select>
                      <Input
                        type="number"
                        value={condition.value}
                        onChange={(e) => updateCondition(i, { value: Number(e.target.value) })}
                        className="w-28"
                      />
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setConditions((prev) => prev.filter((_, idx) => idx !== i))}
                        disabled={conditions.length === 1}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  ))}
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => setConditions((prev) => [...prev, { field: "close", operator: ">", value: 0 }])}
                  >
                    <Plus className="h-3.5 w-3.5" /> Add condition
                  </Button>
                </div>

                <div className="flex items-center gap-2 border-t border-border pt-4">
                  <Button onClick={() => runMutation.mutate()} disabled={runMutation.isPending}>
                    {runMutation.isPending ? "Scanning..." : "Run Scan"}
                  </Button>
                  <Input
                    placeholder="Scan name to save..."
                    value={scanName}
                    onChange={(e) => setScanName(e.target.value)}
                    className="w-48"
                  />
                  <Button variant="secondary" onClick={() => saveMutation.mutate()} disabled={!scanName || saveMutation.isPending}>
                    Save
                  </Button>
                </div>

                {runMutation.error && (
                  <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
                    {runMutation.error instanceof ApiError ? runMutation.error.message : "Scan failed"}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Saved Scans</CardTitle>
              </CardHeader>
              <CardContent className="space-y-1">
                {!savedScans?.length && <EmptyState title="No saved scans yet" />}
                {savedScans?.map((scan) => (
                  <div key={scan.id} className="flex items-center justify-between rounded-md px-2 py-1.5 hover:bg-surface-elevated">
                    <button onClick={() => loadSavedScan(scan)} className="text-left text-sm text-text-secondary hover:text-text-primary">
                      {scan.name}
                    </button>
                    <button onClick={() => deleteMutation.mutate(scan.id)} className="text-text-muted hover:text-negative">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </CardContent>
            </Card>
          </div>

          {result && (
            <Card>
              <CardHeader>
                <CardTitle>
                  Results -- {result.matched.length} of {result.scanned_count} instruments matched
                </CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                {!result.matched.length ? (
                  <EmptyState title="No matches" description="No instruments matched these conditions." />
                ) : (
                  <Table>
                    <Thead>
                      <tr>
                        <Th>Symbol</Th>
                        <Th>Market</Th>
                        <Th>Name</Th>
                        {conditions.map((c) => (
                          <Th key={c.field} className="text-right">
                            {c.field}
                          </Th>
                        ))}
                        <Th />
                      </tr>
                    </Thead>
                    <Tbody>
                      {result.matched.map((m) => (
                        <tr key={m.instrument.id}>
                          <Td className="font-medium">{m.instrument.symbol}</Td>
                          <Td>
                            <Badge tone="neutral">{marketLabel(m.instrument.exchange)}</Badge>
                          </Td>
                          <Td className="text-text-secondary">{m.instrument.name}</Td>
                          {conditions.map((c) => (
                            <Td key={c.field} className="text-right font-financial">
                              {m.values[c.field] !== null && m.values[c.field] !== undefined
                                ? m.values[c.field]!.toFixed(2)
                                : "--"}
                            </Td>
                          ))}
                          <Td className="text-right">
                            <Button variant="destructive" size="sm" onClick={() => setFireOrderInstrument(m.instrument)}>
                              Fire Order
                            </Button>
                          </Td>
                        </tr>
                      ))}
                    </Tbody>
                  </Table>
                )}
              </CardContent>
            </Card>
          )}
        </>
      )}

      {mode === "strategy" && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Run a Python Strategy Across the Market</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex gap-3">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-text-secondary">Strategy</label>
                  <Select value={strategyId} onChange={(e) => selectStrategy(e.target.value)} className="w-64">
                    <option value="">Select a Python strategy...</option>
                    {pythonStrategies.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name}
                      </option>
                    ))}
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-text-secondary">Exchange</label>
                  <Select value={strategyExchange} onChange={(e) => setStrategyExchange(e.target.value)} className="w-36">
                    <option value="">All Markets</option>
                    <option value="NSE">NSE Markets</option>
                    <option value="DELTA">Delta Markets</option>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-text-secondary">Timeframe</label>
                  <Select value={strategyTimeframe} onChange={(e) => setStrategyTimeframe(e.target.value)} className="w-24">
                    {TIMEFRAMES.map((tf) => (
                      <option key={tf} value={tf}>
                        {tf}
                      </option>
                    ))}
                  </Select>
                </div>
              </div>

              {!pythonStrategies.length && (
                <p className="text-sm text-text-muted">
                  You don&apos;t have any Python-coded strategies yet -- build one in the Strategy Builder first.
                </p>
              )}

              <Button onClick={() => runStrategyMutation.mutate()} disabled={!strategyId || runStrategyMutation.isPending}>
                {runStrategyMutation.isPending ? "Scanning..." : "Run Scan"}
              </Button>

              {runStrategyMutation.error && (
                <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
                  {runStrategyMutation.error instanceof ApiError ? runStrategyMutation.error.message : "Scan failed"}
                </div>
              )}
            </CardContent>
          </Card>

          {strategyResult && (
            <Card>
              <CardHeader>
                <div className="space-y-2">
                  <CardTitle>
                    Results -- {strategyResult.matched.length} of {strategyResult.scanned_count} instruments have an active signal
                  </CardTitle>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-xs text-text-muted">
                      Version {strategyResult.strategy_version_number} params:
                    </span>
                    {Object.keys(strategyResult.parameters).length === 0 ? (
                      <span className="text-xs text-text-muted">(none -- strategy uses its code&apos;s own defaults)</span>
                    ) : (
                      Object.entries(strategyResult.parameters).map(([name, value]) => (
                        <Badge key={name} tone="neutral">
                          {name}={value}
                        </Badge>
                      ))
                    )}
                  </div>
                </div>
              </CardHeader>
              <CardContent className="p-0">
                {!strategyResult.matched.length ? (
                  <EmptyState title="No active signals" description="No instruments currently show a BUY/SELL/SHORT signal from this strategy." />
                ) : (
                  <Table>
                    <Thead>
                      <tr>
                        <Th>Symbol</Th>
                        <Th>Market</Th>
                        <Th>Name</Th>
                        <Th className="text-right">Signal</Th>
                        <Th />
                      </tr>
                    </Thead>
                    <Tbody>
                      {strategyResult.matched.map((m) => (
                        <tr key={m.instrument.id}>
                          <Td className="font-medium">{m.instrument.symbol}</Td>
                          <Td>
                            <Badge tone="neutral">{marketLabel(m.instrument.exchange)}</Badge>
                          </Td>
                          <Td className="text-text-secondary">{m.instrument.name}</Td>
                          <Td className="text-right">
                            <Badge tone={signalTone(m.signal)}>{m.signal}</Badge>
                          </Td>
                          <Td className="text-right">
                            <Button variant="destructive" size="sm" onClick={() => setFireOrderInstrument(m.instrument)}>
                              Fire Order
                            </Button>
                          </Td>
                        </tr>
                      ))}
                    </Tbody>
                  </Table>
                )}
                {strategyResult.skipped_symbols.length > 0 && (
                  <p className="px-4 py-3 text-xs text-text-muted">
                    {strategyResult.skipped_symbols.length} instrument(s) skipped (not enough history, or the strategy errored on their data).
                  </p>
                )}
              </CardContent>
            </Card>
          )}
        </>
      )}

      <FireOrderModal key={fireOrderInstrument?.id ?? "none"} instrument={fireOrderInstrument} onClose={() => setFireOrderInstrument(null)} />
    </div>
  );
}
