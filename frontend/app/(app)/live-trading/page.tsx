"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertOctagon, CheckCircle2, Play, Square, XCircle, Zap } from "lucide-react";
import { useState } from "react";

import { LiveTradingBanner } from "@/components/layout/live-trading-banner";
import { MarketContextBar, type DataStatus } from "@/components/trading/market-context-bar";
import { StrategyInstrumentPicker } from "@/components/trading/strategy-instrument-picker";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Select } from "@/components/ui/select";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import {
  useBrokerAccounts,
  useInstruments,
  useKillSwitch,
  useLiveDeployments,
  useLiveOrders,
  useSafetyCheck,
  useStrategies,
} from "@/lib/hooks";
import { marketLabel } from "@/lib/market";
import { TIMEFRAMES } from "@/lib/types";
import type { InstrumentOut, LiveDeploymentOut, LiveEvaluationOut, StrategyOut } from "@/lib/types";

// A broker deals in exactly one settlement currency -- this mirrors the
// backend's broker-code -> currency map (app/api/v1/endpoints/live_trading.py)
// purely for display before a deployment exists to read `currency` back from.
const BROKER_CURRENCY: Record<string, string> = { delta_exchange: "USD", zerodha_kite: "INR" };

function lastEvaluatedDataStatus(lastEvaluatedAt: string | null): DataStatus | undefined {
  if (!lastEvaluatedAt) return undefined;
  const ageSeconds = (Date.now() - new Date(lastEvaluatedAt).getTime()) / 1000;
  return ageSeconds < 60 ? "live" : "stale";
}

function KillSwitchPanel() {
  const { hasRole } = useAuth();
  const { data: killSwitch } = useKillSwitch();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");

  const activateMutation = useMutation({
    mutationFn: () => apiFetch("/api/v1/live-trading/kill-switch/activate", { method: "POST", body: JSON.stringify({ reason }) }),
    onSuccess: () => {
      setReason("");
      queryClient.invalidateQueries({ queryKey: ["kill-switch"] });
      queryClient.invalidateQueries({ queryKey: ["live-deployments"] });
    },
  });
  const deactivateMutation = useMutation({
    mutationFn: () => apiFetch("/api/v1/live-trading/kill-switch/deactivate", { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["kill-switch"] }),
  });

  return (
    <Card className={killSwitch?.active ? "border-critical" : undefined}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <AlertOctagon className="h-4 w-4" /> Global Emergency Stop
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center gap-2">
          <Badge tone={killSwitch?.active ? "critical" : "positive"}>{killSwitch?.active ? "ACTIVE" : "Inactive"}</Badge>
          {killSwitch?.active && killSwitch.reason && <span className="text-sm text-text-secondary">{killSwitch.reason}</span>}
        </div>
        {hasRole("administrator") && (
          <>
            {!killSwitch?.active ? (
              <div className="flex gap-2">
                <Input placeholder="Reason for activating..." value={reason} onChange={(e) => setReason(e.target.value)} />
                <Button variant="destructive" onClick={() => activateMutation.mutate()} disabled={!reason || activateMutation.isPending}>
                  Activate Kill Switch
                </Button>
              </div>
            ) : (
              <Button variant="secondary" onClick={() => deactivateMutation.mutate()} disabled={deactivateMutation.isPending}>
                Deactivate
              </Button>
            )}
          </>
        )}
        <p className="text-xs text-text-muted">Activating immediately stops all active live deployments and blocks new orders until deactivated.</p>
      </CardContent>
    </Card>
  );
}

function StartLiveDeploymentModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data: strategies } = useStrategies();
  const approvedStrategies = strategies?.filter((s) => s.status === "approved");
  const [strategy, setStrategy] = useState<StrategyOut | null>(null);
  const { data: brokerAccounts } = useBrokerAccounts();
  const [brokerAccountId, setBrokerAccountId] = useState("");
  const [instrumentQuery, setInstrumentQuery] = useState("");
  const [instrument, setInstrument] = useState<InstrumentOut | null>(null);
  const { data: rawInstrumentResults } = useInstruments(instrumentQuery);
  // NSE/Yahoo is hidden app-wide -- no reachable data source in production.
  const instrumentResults = rawInstrumentResults?.filter((i) => i.data_source !== "yahoo_nse");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [timeframe, setTimeframe] = useState("1d");
  const [allocatedCapital, setAllocatedCapital] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  const strategyInstrumentIds = strategy?.latest_version?.instrument_ids ?? [];
  const usesStrategyInstruments = strategyInstrumentIds.length > 0;
  const selectedBroker = brokerAccounts?.find((a) => a.id === brokerAccountId);
  const currency = selectedBroker ? BROKER_CURRENCY[selectedBroker.broker.code] : undefined;

  const { data: safety } = useSafetyCheck(strategy?.id ?? null, brokerAccountId || null);

  const startMutation = useMutation({
    mutationFn: async () => {
      const targetIds = usesStrategyInstruments ? [...selectedIds] : instrument ? [instrument.id] : [];
      const perInstrumentCapital = allocatedCapital ? Number(allocatedCapital) / targetIds.length : undefined;
      const results = await Promise.allSettled(
        targetIds.map((instrument_id) =>
          apiFetch<LiveDeploymentOut>("/api/v1/live-trading/deployments", {
            method: "POST",
            body: JSON.stringify({
              strategy_id: strategy!.id,
              instrument_id,
              broker_account_id: brokerAccountId,
              timeframe,
              confirmed: true,
              allocated_capital: perInstrumentCapital,
            }),
          }),
        ),
      );
      const failed = results.filter((r): r is PromiseRejectedResult => r.status === "rejected");
      if (failed.length) {
        const first = failed[0].reason;
        throw first instanceof ApiError ? first : new Error(`${failed.length} of ${targetIds.length} deployments failed to start`);
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["live-deployments"] });
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
      onClose();
    },
  });

  const canStart = (usesStrategyInstruments ? selectedIds.size > 0 : !!instrument) && !!brokerAccountId && confirmed && !!safety?.passed;

  return (
    <Modal open={open} onClose={onClose} title="Start Live Trading">
      <div className="space-y-4">
        <div className="rounded-md bg-critical-soft px-3 py-2 text-xs text-critical">
          This places real orders on your real broker account using real money. Review the safety checklist below carefully.
        </div>

        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Strategy (must be Approved)</label>
          <Select
            value={strategy?.id ?? ""}
            onChange={(e) => {
              const next = approvedStrategies?.find((s) => s.id === e.target.value) ?? null;
              setStrategy(next);
              setInstrument(null);
              setSelectedIds(new Set());
              setTimeframe(next?.latest_version?.timeframe ?? "1d");
            }}
          >
            <option value="" disabled>
              Select an approved strategy
            </option>
            {approvedStrategies?.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </Select>
          {!approvedStrategies?.length && (
            <p className="text-xs text-text-muted">No approved strategies. Backtest, paper trade, then mark-validated + approve a strategy first.</p>
          )}
        </div>

        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Broker Account</label>
          <Select value={brokerAccountId} onChange={(e) => setBrokerAccountId(e.target.value)}>
            <option value="" disabled>
              Select a connected broker account
            </option>
            {brokerAccounts?.filter((a) => a.connection_status === "connected").map((a) => (
              <option key={a.id} value={a.id}>
                {a.broker.name} -- {a.account_label}
              </option>
            ))}
          </Select>
        </div>

        {strategy && usesStrategyInstruments && (
          <StrategyInstrumentPicker
            key={strategy.id}
            strategyVersionInstrumentIds={strategyInstrumentIds}
            selectedIds={selectedIds}
            onChange={setSelectedIds}
          />
        )}

        {strategy && !usesStrategyInstruments && (
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-text-secondary">Instrument</label>
            <p className="text-xs text-text-muted">This strategy wasn&apos;t built with any instruments attached -- pick one to deploy it against.</p>
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
                    {i.symbol} ({marketLabel(i.exchange)})
                  </button>
                ))}
              </div>
            )}
            {instrument && <Badge tone="active">{instrument.symbol}</Badge>}
          </div>
        )}

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
          <div className="flex-1 space-y-1.5">
            <label className="text-sm font-medium text-text-secondary">
              Allocated Capital {currency && <Badge tone="neutral">{currency}</Badge>}
              <span className="ml-1 font-normal text-text-muted">(optional)</span>
            </label>
            <Input
              type="number"
              min="0"
              placeholder="Full broker balance if left blank"
              value={allocatedCapital}
              onChange={(e) => setAllocatedCapital(e.target.value)}
            />
            <p className="text-xs text-text-muted">
              A cap on how much of your real broker balance this deployment may use -- never a separate wallet, the
              broker always holds the real money. Leave blank to size off your full available balance.
              {usesStrategyInstruments && selectedIds.size > 1 && " Split evenly across the selected instruments."}
            </p>
          </div>
        </div>

        {safety && (
          <div className="space-y-1 rounded-md border border-border p-3">
            <p className="text-xs font-semibold uppercase tracking-wide text-text-muted">Safety Checklist</p>
            {Object.entries(safety.checks).map(([name, passed]) => (
              <div key={name} className="flex items-center gap-2 text-sm">
                {passed ? <CheckCircle2 className="h-3.5 w-3.5 text-positive" /> : <XCircle className="h-3.5 w-3.5 text-negative" />}
                <span className={passed ? "text-text-secondary" : "text-negative"}>{name.replace(/_/g, " ")}</span>
              </div>
            ))}
          </div>
        )}

        <label className="flex items-start gap-2 text-sm text-text-secondary">
          <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} className="mt-0.5" />
          I understand this strategy will place real orders with real money on my connected broker account.
        </label>

        {startMutation.error && (
          <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
            {startMutation.error instanceof ApiError ? startMutation.error.message : "Failed to start"}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={() => startMutation.mutate()} disabled={!canStart || startMutation.isPending}>
            {startMutation.isPending
              ? "Starting..."
              : usesStrategyInstruments && selectedIds.size > 1
                ? `Go Live (${selectedIds.size} instruments)`
                : "Go Live"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function DeploymentDetail({ deployment }: { deployment: LiveDeploymentOut }) {
  const { data: orders } = useLiveOrders(deployment.id);
  const { data: instruments } = useInstruments("");
  const { data: brokerAccounts } = useBrokerAccounts();
  const instrument = instruments?.find((i) => i.id === deployment.instrument_id);
  const brokerAccount = brokerAccounts?.find((a) => a.id === deployment.broker_account_id);

  const contextBar = instrument && (
    <MarketContextBar
      broker={brokerAccount?.broker.name ?? "--"}
      market={marketLabel(instrument.exchange)}
      instrument={instrument.symbol}
      instrumentType={instrument.instrument_type.replace("_", " ")}
      timeframe={deployment.timeframe}
      mode="Live"
      dataStatus={brokerAccount && brokerAccount.connection_status !== "connected" ? "disconnected" : lastEvaluatedDataStatus(deployment.last_evaluated_at)}
    />
  );

  if (!orders?.length) {
    return (
      <div className="space-y-4 border-t border-border bg-surface-elevated/50 p-4">
        {contextBar}
        <EmptyState title="No orders yet" />
      </div>
    );
  }

  return (
    <div className="space-y-4 border-t border-border bg-surface-elevated/50 p-4">
      {contextBar}
      <Table>
        <Thead>
          <tr>
            <Th>Side</Th>
            <Th className="text-right">Qty</Th>
            <Th>Status</Th>
            <Th>Broker Order ID</Th>
            <Th>Reason</Th>
          </tr>
        </Thead>
        <Tbody>
          {orders.map((o) => (
            <tr key={o.id}>
              <Td className="uppercase text-text-secondary">{o.side}</Td>
              <Td className="text-right font-financial">{o.quantity}</Td>
              <Td>
                <Badge tone={o.status === "filled" ? "positive" : o.status === "rejected" ? "critical" : "active"}>{o.status}</Badge>
              </Td>
              <Td className="font-financial text-text-muted">{o.broker_order_id ?? "--"}</Td>
              <Td className="text-xs text-text-muted">{o.reason ?? "--"}</Td>
            </tr>
          ))}
        </Tbody>
      </Table>
    </div>
  );
}

function DeploymentRow({ deployment }: { deployment: LiveDeploymentOut }) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [lastEval, setLastEval] = useState<LiveEvaluationOut | null>(null);

  const evaluateMutation = useMutation({
    mutationFn: () => apiFetch<LiveEvaluationOut>(`/api/v1/live-trading/deployments/${deployment.id}/evaluate`, { method: "POST" }),
    onSuccess: (data) => {
      setLastEval(data);
      queryClient.invalidateQueries({ queryKey: ["live-deployments"] });
      queryClient.invalidateQueries({ queryKey: ["live-orders", deployment.id] });
    },
  });
  const stopMutation = useMutation({
    mutationFn: () => apiFetch(`/api/v1/live-trading/deployments/${deployment.id}/stop`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["live-deployments"] }),
  });

  return (
    <>
      <tr className="cursor-pointer hover:bg-surface-elevated" onClick={() => setExpanded(!expanded)}>
        <Td className="font-medium">{deployment.strategy_name}</Td>
        <Td>{deployment.instrument_symbol}</Td>
        <Td className="font-financial text-xs">
          {deployment.allocated_capital != null ? (
            <>
              {deployment.allocated_capital.toFixed(2)} <Badge tone="neutral">{deployment.currency}</Badge>
            </>
          ) : (
            <span className="text-text-muted">full balance</span>
          )}
        </Td>
        <Td>
          <Badge tone={deployment.status === "active" ? "positive" : "inactive"}>{deployment.status}</Badge>
        </Td>
        <Td>
          {deployment.open_position ? (
            <span className="font-financial">
              {deployment.open_position.quantity} @ {deployment.open_position.avg_entry_price.toFixed(2)}
            </span>
          ) : (
            <span className="text-text-muted">flat</span>
          )}
        </Td>
        <Td className="text-xs text-text-muted">{lastEval ? `${lastEval.action} (${lastEval.signal ?? "-"})` : "--"}</Td>
        <Td className="text-right" onClick={(e) => e.stopPropagation()}>
          {deployment.status === "active" && (
            <div className="flex justify-end gap-1">
              <Button variant="ghost" size="sm" onClick={() => evaluateMutation.mutate()} disabled={evaluateMutation.isPending}>
                <Zap className="h-3.5 w-3.5" /> Evaluate Now
              </Button>
              <Button variant="ghost" size="sm" onClick={() => stopMutation.mutate()} disabled={stopMutation.isPending}>
                <Square className="h-3.5 w-3.5" /> Stop
              </Button>
            </div>
          )}
        </Td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={7} className="p-0">
            <DeploymentDetail deployment={deployment} />
          </td>
        </tr>
      )}
    </>
  );
}

export default function LiveTradingPage() {
  const { data: deployments, isLoading } = useLiveDeployments();
  const [modalOpen, setModalOpen] = useState(false);

  return (
    <div className="space-y-6">
      <LiveTradingBanner />

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Live Trading</h1>
          <p className="text-sm text-text-muted">Real orders through the connected Delta Exchange broker, gated by the safety checklist and risk engine.</p>
        </div>
        <Button variant="destructive" onClick={() => setModalOpen(true)}>
          <Play className="h-3.5 w-3.5" /> Go Live
        </Button>
      </div>

      <KillSwitchPanel />

      <Card>
        <CardHeader>
          <CardTitle>Live Deployments</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <LoadingState />
          ) : !deployments?.length ? (
            <EmptyState title="No live deployments yet" description="Go live to begin real execution through your connected broker." />
          ) : (
            <Table>
              <Thead>
                <tr>
                  <Th>Strategy</Th>
                  <Th>Instrument</Th>
                  <Th>Capital</Th>
                  <Th>Status</Th>
                  <Th>Position</Th>
                  <Th>Last Signal</Th>
                  <Th />
                </tr>
              </Thead>
              <Tbody>
                {deployments.map((d) => (
                  <DeploymentRow key={d.id} deployment={d} />
                ))}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>

      <StartLiveDeploymentModal open={modalOpen} onClose={() => setModalOpen(false)} />
    </div>
  );
}
