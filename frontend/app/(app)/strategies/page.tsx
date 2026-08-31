"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { Pencil, Trash2 } from "lucide-react";
import { useState } from "react";

import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/data-state";
import { Modal } from "@/components/ui/modal";
import { apiFetch, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useBacktestResult, useBacktestsForStrategy, useStrategies } from "@/lib/hooks";
import type { StrategyOut, StrategyStatus } from "@/lib/types";

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

function DeleteStrategyModal({ strategy, onClose }: { strategy: StrategyOut; onClose: () => void }) {
  const queryClient = useQueryClient();
  const deleteMutation = useMutation({
    mutationFn: () => apiFetch(`/api/v1/strategies/${strategy.id}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
      onClose();
    },
  });

  return (
    <Modal open onClose={onClose} title={`Delete: ${strategy.name}`}>
      <div className="space-y-4">
        <p className="text-sm text-text-secondary">
          This permanently deletes the strategy, its versions, and any backtest or optimization results. This cannot be
          undone.
        </p>

        {deleteMutation.isError && (
          <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
            {deleteMutation.error instanceof ApiError ? deleteMutation.error.message : "Failed to delete strategy"}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={deleteMutation.isPending}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={() => deleteMutation.mutate()} disabled={deleteMutation.isPending}>
            {deleteMutation.isPending ? "Deleting..." : "Delete Strategy"}
          </Button>
        </div>
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

function StrategyCard({
  strategy,
  canEdit,
  onDelete,
}: {
  strategy: StrategyOut;
  canEdit: boolean;
  onDelete: () => void;
}) {
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
        <div className="flex items-center gap-1">
          {canEdit ? (
            <Link href={`/strategy-builder?id=${strategy.id}`}>
              <Button variant="ghost" size="sm">
                <Pencil className="h-3.5 w-3.5" /> Edit
              </Button>
            </Link>
          ) : (
            <span className="px-2 text-text-muted">View only</span>
          )}
          {canEdit && (
            <Button variant="ghost" size="sm" onClick={onDelete} className="text-text-muted hover:text-negative" title="Delete strategy">
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}

function StrategyGrid({
  title,
  description,
  strategies,
  hasRole,
  user,
  onDelete,
}: {
  title: string;
  description: string;
  strategies: StrategyOut[];
  hasRole: (...roles: string[]) => boolean;
  user: { id: string } | null | undefined;
  onDelete: (s: StrategyOut) => void;
}) {
  if (!strategies.length) return null;
  return (
    <div className="space-y-3">
      <div>
        <h2 className="text-sm font-semibold text-text-primary">{title}</h2>
        <p className="text-xs text-text-muted">{description}</p>
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {strategies.map((s) => (
          <StrategyCard
            key={s.id}
            strategy={s}
            canEdit={s.owner_id === user?.id || hasRole("administrator")}
            onDelete={() => onDelete(s)}
          />
        ))}
      </div>
    </div>
  );
}

export default function StrategiesPage() {
  const { hasRole, user } = useAuth();
  const { data: strategies, isLoading, isError } = useStrategies();
  const [toDelete, setToDelete] = useState<StrategyOut | null>(null);
  const queryClient = useQueryClient();

  const pythonStrategies = strategies?.filter((s) => s.code_type === "python") ?? [];
  const visualStrategies = strategies?.filter((s) => s.code_type === "visual") ?? [];

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
        <div className="space-y-8">
          <StrategyGrid
            title="Python Strategies"
            description="Sandboxed generate_signal(candles, params) code."
            strategies={pythonStrategies}
            hasRole={hasRole}
            user={user}
            onDelete={setToDelete}
          />
          <StrategyGrid
            title="Indicator-Based (Visual) Strategies"
            description="Built from field/operator/value entry and exit conditions."
            strategies={visualStrategies}
            hasRole={hasRole}
            user={user}
            onDelete={setToDelete}
          />
        </div>
      )}

      {toDelete && <DeleteStrategyModal strategy={toDelete} onClose={() => setToDelete(null)} />}
    </div>
  );
}
