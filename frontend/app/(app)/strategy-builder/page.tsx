"use client";

import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { Plus, Trash2, X } from "lucide-react";
import { useState } from "react";

import { InstrumentMultiSelect } from "@/components/market-data/instrument-multiselect";
import { WatchlistLoader } from "@/components/market-data/watchlist-loader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { apiFetch, ApiError } from "@/lib/api";
import { useIndicatorList } from "@/lib/hooks";
import type { InstrumentOut, ScanCondition, ScanOperator, StrategyOut } from "@/lib/types";

const RAW_FIELDS = ["open", "high", "low", "close", "volume"];
const OPERATORS: ScanOperator[] = [">", "<", ">=", "<=", "=="];

const DEFAULT_PYTHON_CODE = `def generate_signal(candles, params):
    """candles: list of {open, high, low, close, volume} oldest-first.
    Must return "BUY", "SELL", or "HOLD"."""
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

export default function StrategyBuilderPage() {
  const router = useRouter();
  const fieldOptions = useFieldOptions();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [timeframe, setTimeframe] = useState("1d");
  const [selectedInstruments, setSelectedInstruments] = useState<InstrumentOut[]>([]);

  function mergeInstruments(loaded: InstrumentOut[]) {
    setSelectedInstruments((prev) => {
      const merged = new Map(prev.map((i) => [i.id, i] as const));
      for (const i of loaded) merged.set(i.id, i);
      return [...merged.values()];
    });
  }

  const [entryConditions, setEntryConditions] = useState<ScanCondition[]>([{ field: "rsi.rsi", operator: ">", value: 55 }]);
  const [exitConditions, setExitConditions] = useState<ScanCondition[]>([{ field: "rsi.rsi", operator: "<", value: 45 }]);
  const [pythonCode, setPythonCode] = useState(DEFAULT_PYTHON_CODE);

  const [sizingType, setSizingType] = useState<"fixed_quantity" | "percent_capital">("fixed_quantity");
  const [sizingValue, setSizingValue] = useState(1);
  const [stopLossPct, setStopLossPct] = useState<string>("");
  const [takeProfitPct, setTakeProfitPct] = useState<string>("");
  const [maxPositions, setMaxPositions] = useState<string>("");

  const createMutation = useMutation({
    mutationFn: (codeType: "visual" | "python") =>
      apiFetch<StrategyOut>("/api/v1/strategies", {
        method: "POST",
        body: JSON.stringify({
          name,
          description: description || null,
          version: {
            timeframe,
            instrument_ids: selectedInstruments.map((i) => i.id),
            parameters: {},
            entry_rules: codeType === "visual" ? { all: entryConditions } : null,
            exit_rules: codeType === "visual" ? { all: exitConditions } : null,
            python_code: codeType === "python" ? pythonCode : null,
            position_sizing: { type: sizingType, value: sizingValue },
            risk_rules: {
              stop_loss_pct: stopLossPct ? Number(stopLossPct) : null,
              take_profit_pct: takeProfitPct ? Number(takeProfitPct) : null,
              max_positions: maxPositions ? Number(maxPositions) : null,
            },
          },
        }),
      }),
    onSuccess: () => router.push("/strategies"),
  });

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Strategy Builder</h1>
        <p className="text-sm text-text-muted">Define rules visually, or import Python code that runs in a sandbox.</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Basics</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-text-secondary">Name</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Momentum Breakout" />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-text-secondary">Description</label>
            <Input value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          <div className="flex gap-4">
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">Timeframe</label>
              <Select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="w-32">
                <option value="1d">1d</option>
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
          <Tabs defaultValue="visual">
            <TabsList>
              <TabsTrigger value="visual">Visual Mode</TabsTrigger>
              <TabsTrigger value="python">Python Code Mode</TabsTrigger>
            </TabsList>
            <TabsContent value="visual">
              <div className="space-y-5">
                <ConditionEditor title="Entry rules" conditions={entryConditions} onChange={setEntryConditions} fieldOptions={fieldOptions} />
                <ConditionEditor title="Exit rules" conditions={exitConditions} onChange={setExitConditions} fieldOptions={fieldOptions} />
                <Button
                  onClick={() => createMutation.mutate("visual")}
                  disabled={!name || createMutation.isPending}
                >
                  {createMutation.isPending ? "Creating..." : "Create Visual Strategy"}
                </Button>
              </div>
            </TabsContent>
            <TabsContent value="python">
              <div className="space-y-3">
                <p className="text-xs text-text-muted">
                  Runs in a sandbox (RestrictedPython + a separate process, no filesystem/network/import access) --
                  must define <code>generate_signal(candles, params)</code> returning &quot;BUY&quot;, &quot;SELL&quot;, or &quot;HOLD&quot;.
                </p>
                <textarea
                  value={pythonCode}
                  onChange={(e) => setPythonCode(e.target.value)}
                  rows={12}
                  className="w-full rounded-md border border-border bg-surface p-3 font-mono text-xs text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
                  spellCheck={false}
                />
                <Button
                  onClick={() => createMutation.mutate("python")}
                  disabled={!name || createMutation.isPending}
                >
                  {createMutation.isPending ? "Creating..." : "Create Python Strategy"}
                </Button>
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
            <Select value={sizingType} onChange={(e) => setSizingType(e.target.value as typeof sizingType)}>
              <option value="fixed_quantity">Fixed Qty</option>
              <option value="percent_capital">% Capital</option>
            </Select>
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">Value</label>
            <Input type="number" value={sizingValue} onChange={(e) => setSizingValue(Number(e.target.value))} />
          </div>
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

      {createMutation.error && (
        <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
          {createMutation.error instanceof ApiError ? createMutation.error.message : "Failed to create strategy"}
        </div>
      )}
    </div>
  );
}
