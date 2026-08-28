"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { CheckCircle2, XCircle } from "lucide-react";
import { useState } from "react";

import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Modal } from "@/components/ui/modal";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useStrategies } from "@/lib/hooks";
import type { StrategyOut, StrategyStatus, ValidateResult } from "@/lib/types";

const STATUS_TONE: Record<StrategyStatus, Tone> = {
  draft: "neutral",
  backtested: "active",
  optimized: "active",
  out_of_sample_tested: "active",
  paper_trading: "warning",
  validated: "positive",
  approved: "positive",
  live: "positive",
};

function ValidateModal({ strategy, onClose }: { strategy: StrategyOut; onClose: () => void }) {
  const [result, setResult] = useState<ValidateResult | null>(null);
  const validateMutation = useMutation({
    mutationFn: () => apiFetch<ValidateResult>(`/api/v1/strategies/${strategy.id}/validate`, { method: "POST" }),
    onSuccess: setResult,
  });

  return (
    <Modal open onClose={onClose} title={`Validate: ${strategy.name}`}>
      <div className="space-y-4">
        <div className="text-sm text-text-secondary">
          <p>
            Mode: <span className="font-medium text-text-primary capitalize">{strategy.code_type}</span>
          </p>
          <p>
            Timeframe: <span className="font-medium text-text-primary">{strategy.latest_version?.timeframe}</span>
          </p>
        </div>

        {strategy.code_type === "python" && (
          <pre className="max-h-48 overflow-auto rounded-md bg-surface-elevated p-3 text-xs text-text-secondary">
            {strategy.latest_version?.python_code}
          </pre>
        )}

        <Button onClick={() => validateMutation.mutate()} disabled={validateMutation.isPending}>
          {validateMutation.isPending ? "Validating..." : "Run Validation"}
        </Button>

        {result && (
          <div
            className={`flex items-start gap-2 rounded-md px-3 py-2 text-sm ${
              result.valid ? "bg-positive-soft text-positive" : "bg-negative-soft text-negative"
            }`}
          >
            {result.valid ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" /> : <XCircle className="mt-0.5 h-4 w-4 shrink-0" />}
            <div>
              {result.valid ? (
                <span>Valid. Sample signal: {result.sample_signal}</span>
              ) : (
                <span>{result.error}</span>
              )}
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

export default function StrategiesPage() {
  const { hasRole } = useAuth();
  const { data: strategies, isLoading } = useStrategies();
  const [selected, setSelected] = useState<StrategyOut | null>(null);
  const queryClient = useQueryClient();

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Strategies</h1>
          <p className="text-sm text-text-muted">Visual rule-based or sandboxed Python strategies.</p>
        </div>
        {hasRole("administrator", "trader", "analyst") && (
          <Link href="/strategy-builder">
            <Button onClick={() => queryClient.invalidateQueries({ queryKey: ["strategies"] })}>New Strategy</Button>
          </Link>
        )}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>All Strategies</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <p className="p-5 text-sm text-text-muted">Loading...</p>
          ) : !strategies?.length ? (
            <p className="p-5 text-sm text-text-muted">
              No strategies yet.{" "}
              <Link href="/strategy-builder" className="text-active hover:underline">
                Create one
              </Link>
              .
            </p>
          ) : (
            <Table>
              <Thead>
                <tr>
                  <Th>Name</Th>
                  <Th>Mode</Th>
                  <Th>Timeframe</Th>
                  <Th>Status</Th>
                  <Th>Updated</Th>
                  <Th />
                </tr>
              </Thead>
              <Tbody>
                {strategies.map((s) => (
                  <tr key={s.id}>
                    <Td className="font-medium">{s.name}</Td>
                    <Td className="capitalize text-text-secondary">{s.code_type}</Td>
                    <Td className="text-text-secondary">{s.latest_version?.timeframe}</Td>
                    <Td>
                      <Badge tone={STATUS_TONE[s.status]}>{s.status.replace(/_/g, " ")}</Badge>
                    </Td>
                    <Td className="text-text-muted">{new Date(s.updated_at).toLocaleDateString()}</Td>
                    <Td className="text-right">
                      <Button variant="ghost" size="sm" onClick={() => setSelected(s)}>
                        Validate
                      </Button>
                    </Td>
                  </tr>
                ))}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>

      {selected && <ValidateModal strategy={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
