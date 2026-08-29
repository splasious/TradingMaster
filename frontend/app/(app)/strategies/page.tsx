"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { CheckCircle2, XCircle } from "lucide-react";
import { useState } from "react";

import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/data-state";
import { Modal } from "@/components/ui/modal";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useBacktestResult, useBacktestsForStrategy, useStrategies } from "@/lib/hooks";
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

function StrategyPerformance({ strategyId }: { strategyId: string }) {
  const { data: jobs } = useBacktestsForStrategy(strategyId);
  const latestCompleted = jobs?.find((j) => j.status === "completed") ?? null;
  const { data: result } = useBacktestResult(latestCompleted?.id ?? null, !!latestCompleted);

  if (!jobs || (jobs.length > 0 && !latestCompleted)) {
    return <span className="text-xs text-text-muted">--</span>;
  }
  if (!latestCompleted || !result) {
    return <span className="text-xs text-text-muted">Not backtested</span>;
  }

  const { net_profit, win_rate_pct, num_trades } = result.metrics;
  return (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
      <span className={`font-financial font-medium ${net_profit >= 0 ? "text-positive" : "text-negative"}`}>
        {net_profit >= 0 ? "+" : ""}
        {net_profit.toLocaleString(undefined, { maximumFractionDigits: 0 })}
      </span>
      <span className="text-text-muted">{win_rate_pct.toFixed(0)}% win</span>
      <span className="text-text-muted">({num_trades} trades)</span>
    </span>
  );
}

function StrategyCard({ strategy, onValidate }: { strategy: StrategyOut; onValidate: () => void }) {
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-text-primary" title={strategy.name}>
            {strategy.name}
          </h3>
          <p className="mt-0.5 text-xs capitalize text-text-muted">
            {strategy.code_type} &middot; {strategy.latest_version?.timeframe ?? "--"}
          </p>
        </div>
        <Badge tone={STATUS_TONE[strategy.status]} className="shrink-0">
          {strategy.status.replace(/_/g, " ")}
        </Badge>
      </div>

      <div className="min-h-[1.25rem]">
        <StrategyPerformance strategyId={strategy.id} />
      </div>

      <div className="mt-auto flex items-center justify-between border-t border-border pt-3 text-xs text-text-muted">
        <span>Updated {new Date(strategy.updated_at).toLocaleDateString()}</span>
        <Button variant="ghost" size="sm" onClick={onValidate}>
          Validate
        </Button>
      </div>
    </Card>
  );
}

export default function StrategiesPage() {
  const { hasRole } = useAuth();
  const { data: strategies, isLoading, isError } = useStrategies();
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

      {isLoading ? (
        <LoadingState />
      ) : isError ? (
        <ErrorState description="Could not load strategies." />
      ) : !strategies?.length ? (
        <EmptyState
          title="No strategies yet"
          action={
            <Link href="/strategy-builder" className="text-sm text-active hover:underline">
              Create one
            </Link>
          }
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {strategies.map((s) => (
            <StrategyCard key={s.id} strategy={s} onValidate={() => setSelected(s)} />
          ))}
        </div>
      )}

      {selected && <ValidateModal strategy={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
