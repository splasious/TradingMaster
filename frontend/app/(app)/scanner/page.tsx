"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import { useIndicatorList } from "@/lib/hooks";
import type { ScanCondition, ScanOperator, ScanResponse, SavedScanOut } from "@/lib/types";

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

export default function ScannerPage() {
  const fieldOptions = useFieldOptions();
  const queryClient = useQueryClient();

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
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Market Scanner</h1>
        <p className="text-sm text-text-muted">Filter the instrument catalog by price, volume, or indicator values.</p>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Filter Conditions</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex gap-3">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-text-secondary">Exchange</label>
                <Select value={exchange} onChange={(e) => setExchange(e.target.value)} className="w-32">
                  <option value="">All</option>
                  <option value="NSE">NSE</option>
                  <option value="DELTA">DELTA</option>
                </Select>
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-text-secondary">Timeframe</label>
                <Select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="w-24">
                  <option value="1d">1d</option>
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
            {!savedScans?.length && <p className="text-sm text-text-muted">No saved scans yet.</p>}
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
              <p className="p-5 text-sm text-text-muted">No instruments matched these conditions.</p>
            ) : (
              <Table>
                <Thead>
                  <tr>
                    <Th>Symbol</Th>
                    <Th>Exchange</Th>
                    <Th>Name</Th>
                    {conditions.map((c) => (
                      <Th key={c.field} className="text-right">
                        {c.field}
                      </Th>
                    ))}
                  </tr>
                </Thead>
                <Tbody>
                  {result.matched.map((m) => (
                    <tr key={m.instrument.id}>
                      <Td className="font-medium">{m.instrument.symbol}</Td>
                      <Td>
                        <Badge tone="neutral">{m.instrument.exchange}</Badge>
                      </Td>
                      <Td className="text-text-secondary">{m.instrument.name}</Td>
                      {conditions.map((c) => (
                        <Td key={c.field} className="text-right font-financial">
                          {m.values[c.field] !== null && m.values[c.field] !== undefined
                            ? m.values[c.field]!.toFixed(2)
                            : "--"}
                        </Td>
                      ))}
                    </tr>
                  ))}
                </Tbody>
              </Table>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
