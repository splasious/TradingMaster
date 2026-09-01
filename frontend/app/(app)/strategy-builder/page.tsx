"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { CheckCircle2, Plus, Trash2, X, XCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { InstrumentMultiSelect } from "@/components/market-data/instrument-multiselect";
import { WatchlistLoader } from "@/components/market-data/watchlist-loader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { apiFetch, ApiError } from "@/lib/api";
import { useIndicatorList, useStrategy } from "@/lib/hooks";
import { TIMEFRAMES } from "@/lib/types";
import type { InstrumentOut, RuleNode, ScanCondition, ScanOperator, StrategyOut, ValidateResult } from "@/lib/types";

const RAW_FIELDS = ["open", "high", "low", "close", "volume"];
const OPERATORS: ScanOperator[] = [">", "<", ">=", "<=", "=="];

const DEFAULT_PYTHON_CODE = `def generate_signal(candles, params):
    """candles: list of {ts, open, high, low, close, volume} oldest-first,
    ts is an ISO8601 UTC timestamp string. Must return "BUY", "SELL", or "HOLD"."""
    if candles[-1]["close"] > candles[0]["close"]:
        return "BUY"
    return "HOLD"
`;

function useFieldOptions() {
  const { data: indicators } = useIndicatorList();
  const options = RAW_FIELDS.map((f) => ({ value: f, label: f }));
  for (const spec of indicators ?? []) {
    for (const output of spec.output_fields) {
      options.push({ value: `${spec.code}.${output}`, label: `${spec.name} (${output})` });
    }
  }
  return options;
}

function ConditionEditor({
  title,
  conditions,
  onChange,
  fieldOptions,
}: {
  title: string;
  conditions: ScanCondition[];
  onChange: (c: ScanCondition[]) => void;
  fieldOptions: { value: string; label: string }[];
}) {
  return (
    <div className="space-y-2">
      <label className="text-xs font-medium uppercase tracking-wide text-text-muted">{title} (all must be true)</label>
      {conditions.map((c, i) => (
        <div key={i} className="flex items-center gap-2">
          <Select value={c.field} onChange={(e) => onChange(conditions.map((x, idx) => (idx === i ? { ...x, field: e.target.value } : x)))} className="flex-1">
            {fieldOptions.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </Select>
          <Select
            value={c.operator}
            onChange={(e) => onChange(conditions.map((x, idx) => (idx === i ? { ...x, operator: e.target.value as ScanOperator } : x)))}
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
            value={c.value}
            onChange={(e) => onChange(conditions.map((x, idx) => (idx === i ? { ...x, value: Number(e.target.value) } : x)))}
            className="w-28"
          />
          <Button variant="ghost" size="sm" onClick={() => onChange(conditions.filter((_, idx) => idx !== i))}>
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      ))}
      <Button variant="secondary" size="sm" onClick={() => onChange([...conditions, { field: "close", operator: ">", value: 0 }])}>
        <Plus className="h-3.5 w-3.5" /> Add condition
      </Button>
    </div>
  );
}

function ruleConditions(rule: RuleNode | null | undefined): ScanCondition[] | null {
  if (rule && "all" in rule) return rule.all as ScanCondition[];
  return null;
}

/** Mounted fresh (keyed by strategy id in the parent) once the strategy to
 * edit -- if any -- has loaded, so every field's initial state can be
 * computed directly from `existing` instead of a useEffect full of
 * setState calls firing after first render. */
function StrategyForm({ editId, existing }: { editId: string | null; existing: StrategyOut | null }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const fieldOptions = useFieldOptions();
  const v = existing?.latest_version ?? null;

  const [name, setName] = useState(existing?.name ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [timeframe, setTimeframe] = useState(v?.timeframe ?? "1d");
  const [selectedInstruments, setSelectedInstruments] = useState<InstrumentOut[]>([]);

  useEffect(() => {
    if (!v?.instrument_ids.length) return;
    let cancelled = false;
    Promise.all(v.instrument_ids.map((id) => apiFetch<InstrumentOut>(`/api/v1/instruments/${id}`).catch(() => null))).then((results) => {
      if (!cancelled) setSelectedInstruments(results.filter((r): r is InstrumentOut => r !== null));
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-fetch if the strategy version identity actually changes
  }, [v?.id]);

  function mergeInstruments(loaded: InstrumentOut[]) {
    setSelectedInstruments((prev) => {
      const merged = new Map(prev.map((i) => [i.id, i] as const));
      for (const i of loaded) merged.set(i.id, i);
      return [...merged.values()];
    });
  }

  const [entryConditions, setEntryConditions] = useState<ScanCondition[]>(
    ruleConditions(v?.entry_rules ?? null) ?? [{ field: "rsi.rsi", operator: ">", value: 55 }],
  );
  const [exitConditions, setExitConditions] = useState<ScanCondition[]>(
    ruleConditions(v?.exit_rules ?? null) ?? [{ field: "rsi.rsi", operator: "<", value: 45 }],
  );
  const [pythonCode, setPythonCode] = useState(v?.python_code ?? DEFAULT_PYTHON_CODE);

  // "By Number of Stocks" is a UI convenience, not a distinct backend
  // sizing type -- it just computes an equal-weight percent_capital value
  // (100 / N) so the strategy always deploys with 100% of the pool spread
  // evenly across however many stocks you say to hold.
  const [sizingMode, setSizingMode] = useState<"fixed_quantity" | "percent_capital" | "stock_count">(
    v?.position_sizing.type ?? "fixed_quantity",
  );
  const [sizingValue, setSizingValue] = useState(v?.position_sizing.value ?? 1);
  const [stockCount, setStockCount] = useState(Math.max(1, v?.instrument_ids.length ?? 1));
  const [stopLossPct, setStopLossPct] = useState<string>(v?.risk_rules.stop_loss_pct?.toString() ?? "");
  const [takeProfitPct, setTakeProfitPct] = useState<string>(v?.risk_rules.take_profit_pct?.toString() ?? "");
  const [maxPositions, setMaxPositions] = useState<string>(v?.risk_rules.max_positions?.toString() ?? "");

  const [validateResult, setValidateResult] = useState<ValidateResult | null>(null);

  const positionSizing =
    sizingMode === "stock_count"
      ? { type: "percent_capital" as const, value: 100 / Math.max(1, stockCount) }
      : { type: sizingMode, value: sizingValue };

  const versionBody = (codeType: "visual" | "python") => ({
    timeframe,
    instrument_ids: selectedInstruments.map((i) => i.id),
    parameters: {},
    entry_rules: codeType === "visual" ? { all: entryConditions } : null,
    exit_rules: codeType === "visual" ? { all: exitConditions } : null,
    python_code: codeType === "python" ? pythonCode : null,
    position_sizing: positionSizing,
    risk_rules: {
      stop_loss_pct: stopLossPct ? Number(stopLossPct) : null,
      take_profit_pct: takeProfitPct ? Number(takeProfitPct) : null,
      max_positions: maxPositions ? Number(maxPositions) : null,
    },
  });

  const createMutation = useMutation({
    mutationFn: (codeType: "visual" | "python") =>
      editId
        ? apiFetch<StrategyOut>(`/api/v1/strategies/${editId}/versions`, {
            method: "POST",
            body: JSON.stringify(versionBody(codeType)),
          })
        : apiFetch<StrategyOut>("/api/v1/strategies", {
            method: "POST",
            body: JSON.stringify({ name, description: description || null, version: versionBody(codeType) }),
          }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
      router.push("/strategies");
    },
  });

  const validateMutation = useMutation({
    mutationFn: () => apiFetch<ValidateResult>(`/api/v1/strategies/${editId}/validate`, { method: "POST" }),
    onSuccess: setValidateResult,
  });

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">{editId ? `Edit: ${existing?.name ?? "Strategy"}` : "Strategy Builder"}</h1>
        <p className="text-sm text-text-muted">
          {editId
            ? "Saving creates a new version -- prior backtests and deployments stay attached to the version they ran on."
            : "Define rules visually, or import Python code that runs in a sandbox."}
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Basics</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-text-secondary">Name</label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Momentum Breakout"
              disabled={!!editId}
              title={editId ? "Renaming isn't supported yet -- edit rules/parameters here instead." : undefined}
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-text-secondary">Description</label>
            <Input value={description} onChange={(e) => setDescription(e.target.value)} disabled={!!editId} />
          </div>
          <div className="flex gap-4">
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">Timeframe</label>
              <Select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="w-32">
                {TIMEFRAMES.map((tf) => (
                  <option key={tf} value={tf}>
                    {tf}
                  </option>
                ))}
              </Select>
            </div>
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-text-secondary">Instruments</label>
            <WatchlistLoader onLoad={mergeInstruments} />
            <InstrumentMultiSelect value={selectedInstruments} onChange={setSelectedInstruments} />
            <div className="flex flex-wrap gap-1.5">
              {selectedInstruments.map((i) => (
                <Badge key={i.id} tone="active">
                  {i.symbol}
                  <button onClick={() => setSelectedInstruments(selectedInstruments.filter((s) => s.id !== i.id))}>
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Rules</CardTitle>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue={existing?.code_type ?? "visual"}>
            <TabsList>
              <TabsTrigger value="visual">Visual Mode</TabsTrigger>
              <TabsTrigger value="python">Python Code Mode</TabsTrigger>
            </TabsList>
            <TabsContent value="visual">
              <div className="space-y-5">
                <ConditionEditor title="Entry rules" conditions={entryConditions} onChange={setEntryConditions} fieldOptions={fieldOptions} />
                <ConditionEditor title="Exit rules" conditions={exitConditions} onChange={setExitConditions} fieldOptions={fieldOptions} />
                <div className="flex items-center gap-2">
                  <Button onClick={() => createMutation.mutate("visual")} disabled={!name || createMutation.isPending}>
                    {createMutation.isPending ? "Saving..." : editId ? "Save Changes" : "Create Visual Strategy"}
                  </Button>
                  {editId && (
                    <Button variant="secondary" onClick={() => validateMutation.mutate()} disabled={validateMutation.isPending}>
                      {validateMutation.isPending ? "Validating..." : "Validate"}
                    </Button>
                  )}
                </div>
              </div>
            </TabsContent>
            <TabsContent value="python">
              <div className="space-y-3">
                <p className="text-xs text-text-muted">
                  Runs in a sandbox (RestrictedPython + a separate process, no filesystem/network/import access, no{" "}
                  <code>import</code> statements at all) -- must define <code>generate_signal(candles, params)</code>{" "}
                  returning &quot;BUY&quot;, &quot;SELL&quot;, or &quot;HOLD&quot;. Called once per bar with{" "}
                  <code>candles</code> as everything up to and including that bar (oldest-first list of{" "}
                  <code>{"{ts, open, high, low, close, volume}"}</code>); position sizing and stop-loss/take-profit
                  above are applied by the backtest engine itself, not by your code.
                </p>
                <textarea
                  value={pythonCode}
                  onChange={(e) => setPythonCode(e.target.value)}
                  rows={12}
                  className="w-full rounded-md border border-border bg-surface p-3 font-mono text-xs text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
                  spellCheck={false}
                />
                <div className="flex items-center gap-2">
                  <Button onClick={() => createMutation.mutate("python")} disabled={!name || createMutation.isPending}>
                    {createMutation.isPending ? "Saving..." : editId ? "Save Changes" : "Create Python Strategy"}
                  </Button>
                  {editId && (
                    <Button variant="secondary" onClick={() => validateMutation.mutate()} disabled={validateMutation.isPending}>
                      {validateMutation.isPending ? "Validating..." : "Validate"}
                    </Button>
                  )}
                </div>
              </div>
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Position Sizing &amp; Risk</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">Sizing</label>
            <Select value={sizingMode} onChange={(e) => setSizingMode(e.target.value as typeof sizingMode)}>
              <option value="fixed_quantity">Fixed Qty</option>
              <option value="percent_capital">% Capital</option>
              <option value="stock_count">By Number of Stocks</option>
            </Select>
          </div>
          {sizingMode === "stock_count" ? (
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-text-secondary">Hold this many stocks</label>
              <Input type="number" min="1" value={stockCount} onChange={(e) => setStockCount(Number(e.target.value))} />
              <p className="text-xs text-text-muted">{(100 / Math.max(1, stockCount)).toFixed(2)}% of capital per position</p>
            </div>
          ) : (
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-text-secondary">Value</label>
              <Input type="number" value={sizingValue} onChange={(e) => setSizingValue(Number(e.target.value))} />
            </div>
          )}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">Stop Loss %</label>
            <Input type="number" value={stopLossPct} onChange={(e) => setStopLossPct(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">Take Profit %</label>
            <Input type="number" value={takeProfitPct} onChange={(e) => setTakeProfitPct(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">Max Positions</label>
            <Input type="number" value={maxPositions} onChange={(e) => setMaxPositions(e.target.value)} />
          </div>
        </CardContent>
      </Card>

      {editId && validateResult && (
        <div
          className={`flex items-start gap-2 rounded-md px-3 py-2 text-sm ${
            validateResult.valid ? "bg-positive-soft text-positive" : "bg-negative-soft text-negative"
          }`}
        >
          {validateResult.valid ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" /> : <XCircle className="mt-0.5 h-4 w-4 shrink-0" />}
          <div>
            {validateResult.valid ? (
              <span>Valid (last saved version). Sample signal: {validateResult.sample_signal}</span>
            ) : (
              <span>{validateResult.error}</span>
            )}
          </div>
        </div>
      )}

      {createMutation.error && (
        <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
          {createMutation.error instanceof ApiError ? createMutation.error.message : "Failed to save strategy"}
        </div>
      )}
    </div>
  );
}

export default function StrategyBuilderPage() {
  const searchParams = useSearchParams();
  const editId = searchParams.get("id");
  const { data: existing, isLoading: isLoadingExisting } = useStrategy(editId);

  if (editId && isLoadingExisting) {
    return <p className="text-sm text-text-muted">Loading strategy...</p>;
  }

  return <StrategyForm key={editId ?? "new"} editId={editId} existing={existing ?? null} />;
}
