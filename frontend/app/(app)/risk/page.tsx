"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { KillSwitchPanel } from "@/components/trading/kill-switch-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/data-state";
import { apiFetch, ApiError } from "@/lib/api";
import { useBrokerAccounts, useLiveDeployments } from "@/lib/hooks";
import type { LiveDeploymentOut, ReconciliationOut } from "@/lib/types";

function DailyLossBar({ deployment }: { deployment: LiveDeploymentOut }) {
  const limitPct = deployment.risk_rules.max_daily_loss_pct;
  if (!limitPct) return <p className="text-xs text-text-muted">No daily loss limit configured</p>;

  const pnl = deployment.realized_pnl_today;
  // pnl is an absolute amount, limitPct is a % of capital -- without a
  // stable capital baseline here (allocated_capital may be null, meaning
  // "full broker balance"), this shows the raw number against the
  // configured % rather than a false-precision progress bar.
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span className="text-text-muted">Today&apos;s P&amp;L</span>
        <span className={pnl >= 0 ? "text-positive" : "text-negative"}>
          {pnl >= 0 ? "+" : ""}
          {pnl.toFixed(2)} {deployment.currency}
        </span>
      </div>
      <p className="text-xs text-text-muted">Limit: {limitPct}% of capital/day</p>
    </div>
  );
}

function ReconcileButton({ accountId, label }: { accountId: string; label: string }) {
  const [result, setResult] = useState<ReconciliationOut | null>(null);
  const mutation = useMutation({
    mutationFn: () => apiFetch<ReconciliationOut>(`/api/v1/live-trading/reconcile?broker_account_id=${accountId}`),
    onSuccess: (data) => setResult(data),
  });

  return (
    <div className="space-y-2 rounded-md border border-border p-3">
      <div className="flex items-center justify-between">
        <span className="text-sm text-text-primary">{label}</span>
        <Button variant="secondary" size="sm" onClick={() => mutation.mutate()} disabled={mutation.isPending}>
          {mutation.isPending ? "Checking..." : "Run Reconciliation Check"}
        </Button>
      </div>
      {mutation.isError && (
        <p className="text-xs text-negative">{mutation.error instanceof ApiError ? mutation.error.message : "Check failed"}</p>
      )}
      {result && (
        <div className="text-xs">
          {result.clean ? (
            <Badge tone="positive">Clean -- broker and local records match</Badge>
          ) : (
            <div className="space-y-1">
              <Badge tone="critical">Mismatch found</Badge>
              <p className="text-text-muted">
                {result.local_only.length} local-only, {result.broker_only.length} broker-only, {result.quantity_mismatches.length} quantity
                mismatch(es)
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function RiskPage() {
  const { data: deployments } = useLiveDeployments();
  const { data: brokerAccounts } = useBrokerAccounts();
  const active = (deployments ?? []).filter((d) => d.status === "active");
  const connectedLiveAccounts = (brokerAccounts ?? []).filter((a) => a.environment === "live" && a.connection_status === "connected");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Risk Management</h1>
        <p className="text-sm text-text-muted">Global emergency stop, per-deployment risk limits, and broker reconciliation.</p>
      </div>

      <KillSwitchPanel />

      <div className="space-y-3">
        <h2 className="text-sm font-semibold text-text-primary">Active Deployment Risk Limits</h2>
        {!active.length ? (
          <EmptyState title="No active live deployments" />
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {active.map((d) => (
              <Card key={d.id}>
                <CardHeader>
                  <CardTitle className="text-sm">
                    {d.instrument_symbol} -- {d.strategy_name}
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="flex flex-wrap gap-1 text-xs">
                    {d.risk_rules.stop_loss_pct != null && <Badge tone="neutral">Stop-loss {d.risk_rules.stop_loss_pct}%</Badge>}
                    {d.risk_rules.take_profit_pct != null && <Badge tone="neutral">Take-profit {d.risk_rules.take_profit_pct}%</Badge>}
                    {d.risk_rules.max_positions != null && <Badge tone="neutral">Max positions {d.risk_rules.max_positions}</Badge>}
                    {!d.risk_rules.stop_loss_pct && !d.risk_rules.take_profit_pct && !d.risk_rules.max_positions && !d.risk_rules.max_daily_loss_pct && (
                      <span className="text-text-muted">No risk limits configured</span>
                    )}
                  </div>
                  <DailyLossBar deployment={d} />
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </div>

      <div className="space-y-3">
        <h2 className="text-sm font-semibold text-text-primary">Broker Reconciliation</h2>
        <p className="text-xs text-text-muted">
          Compares what TradingMaster&apos;s database thinks is open against what the broker actually reports -- not run
          automatically, check manually after anything unusual.
        </p>
        {!connectedLiveAccounts.length ? (
          <p className="text-sm text-text-muted">No connected live broker accounts yet.</p>
        ) : (
          <div className="space-y-2">
            {connectedLiveAccounts.map((account) => (
              <ReconcileButton key={account.id} accountId={account.id} label={`${account.broker.name} -- ${account.account_label}`} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
