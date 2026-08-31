"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Play, Square, Trash2, Zap } from "lucide-react";
import { useState } from "react";

import { PaperTradingBanner } from "@/components/layout/environment-mode-banner";
import { MarketContextBar, type DataStatus } from "@/components/trading/market-context-bar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Select } from "@/components/ui/select";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import {
  useInstruments,
  usePaperDeployments,
  usePaperOrders,
  usePaperPortfolio,
  usePaperTrades,
  useStrategies,
} from "@/lib/hooks";
import { marketLabel } from "@/lib/market";
import type { InstrumentOut, PaperDeploymentOut, PaperEvaluationOut, StrategyOut } from "@/lib/types";

function lastEvaluatedDataStatus(lastEvaluatedAt: string | null): DataStatus | undefined {
  if (!lastEvaluatedAt) return undefined;
  const ageSeconds = (Date.now() - new Date(lastEvaluatedAt).getTime()) / 1000;
  return ageSeconds < 60 ? "live" : "stale";
}

function StartDeploymentModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data: strategies } = useStrategies();
  const [strategy, setStrategy] = useState<StrategyOut | null>(null);
  const [instrumentQuery, setInstrumentQuery] = useState("");
  const [instrument, setInstrument] = useState<InstrumentOut | null>(null);
  const { data: instrumentResults } = useInstruments(instrumentQuery);

  const startMutation = useMutation({
    mutationFn: () =>
      apiFetch<PaperDeploymentOut>("/api/v1/paper-trading/deployments", {
        method: "POST",
        body: JSON.stringify({ strategy_id: strategy!.id, instrument_id: instrument!.id, timeframe: "1d" }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["paper-deployments"] });
      onClose();
    },
  });

  return (
    <Modal open={open} onClose={onClose} title="Start Paper Trading">
      <div className="space-y-4">
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Strategy</label>
          <Select value={strategy?.id ?? ""} onChange={(e) => setStrategy(strategies?.find((s) => s.id === e.target.value) ?? null)}>
            <option value="" disabled>
              Select a strategy
            </option>
            {strategies?.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({s.code_type})
              </option>
            ))}
          </Select>
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
                  {i.symbol} ({marketLabel(i.exchange)})
                </button>
              ))}
            </div>
          )}
          {instrument && <Badge tone="active">{instrument.symbol}</Badge>}
        </div>

        {startMutation.error && (
          <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
            {startMutation.error instanceof ApiError ? startMutation.error.message : "Failed to start"}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => startMutation.mutate()} disabled={!strategy || !instrument || startMutation.isPending}>
            {startMutation.isPending ? "Starting..." : "Start"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function DeleteDeploymentModal({ deployment, onClose }: { deployment: PaperDeploymentOut; onClose: () => void }) {
  const queryClient = useQueryClient();
  const deleteMutation = useMutation({
    mutationFn: () => apiFetch(`/api/v1/paper-trading/deployments/${deployment.id}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["paper-deployments"] });
      onClose();
    },
  });

  return (
    <Modal open onClose={onClose} title={`Delete: ${deployment.strategy_name} on ${deployment.instrument_symbol}`}>
      <div className="space-y-4">
        <p className="text-sm text-text-secondary">
          This permanently deletes the deployment and its simulated order/trade history. This cannot be undone.
        </p>

        {deleteMutation.isError && (
          <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
            {deleteMutation.error instanceof ApiError ? deleteMutation.error.message : "Failed to delete deployment"}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={deleteMutation.isPending}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={() => deleteMutation.mutate()} disabled={deleteMutation.isPending}>
            {deleteMutation.isPending ? "Deleting..." : "Delete Deployment"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function DeploymentDetail({ deployment }: { deployment: PaperDeploymentOut }) {
  const { data: orders } = usePaperOrders(deployment.id);
  const { data: trades } = usePaperTrades(deployment.id);
  const { data: instruments } = useInstruments("");
  const instrument = instruments?.find((i) => i.id === deployment.instrument_id);

  return (
    <div className="space-y-4 border-t border-border bg-surface-elevated/50 p-4">
      {instrument && (
        <MarketContextBar
          broker="Simulated"
          market={marketLabel(instrument.exchange)}
          instrument={instrument.symbol}
          instrumentType={instrument.instrument_type.replace("_", " ")}
          timeframe={deployment.timeframe}
          mode="Paper"
          dataStatus={lastEvaluatedDataStatus(deployment.last_evaluated_at)}
        />
      )}
      {trades && trades.length > 0 && (
        <div>
          <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">Closed Trades</h4>
          <Table>
            <Thead>
              <tr>
                <Th>Entry</Th>
                <Th>Exit</Th>
                <Th className="text-right">Qty</Th>
                <Th className="text-right">PnL</Th>
              </tr>
            </Thead>
            <Tbody>
              {trades.map((t, i) => (
                <tr key={i}>
                  <Td className="font-financial">{t.entry_price.toFixed(2)}</Td>
                  <Td className="font-financial">{t.exit_price.toFixed(2)}</Td>
                  <Td className="text-right font-financial">{t.quantity}</Td>
                  <Td className={`text-right font-financial ${t.pnl >= 0 ? "text-positive" : "text-negative"}`}>{t.pnl.toFixed(2)}</Td>
                </tr>
              ))}
            </Tbody>
          </Table>
        </div>
      )}
      {orders && orders.length > 0 && (
        <div>
          <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">Order History</h4>
          <Table>
            <Thead>
              <tr>
                <Th>Side</Th>
                <Th className="text-right">Qty</Th>
                <Th className="text-right">Price</Th>
                <Th>Status</Th>
                <Th>Reason</Th>
              </tr>
            </Thead>
            <Tbody>
              {orders.map((o) => (
                <tr key={o.id}>
                  <Td className="uppercase text-text-secondary">{o.side}</Td>
                  <Td className="text-right font-financial">{o.quantity}</Td>
                  <Td className="text-right font-financial">{o.price.toFixed(2)}</Td>
                  <Td>
                    <Badge tone={o.status === "filled" ? "positive" : "critical"}>{o.status}</Badge>
                  </Td>
                  <Td className="text-xs text-text-muted">{o.reason ?? "--"}</Td>
                </tr>
              ))}
            </Tbody>
          </Table>
        </div>
      )}
      {!orders?.length && !trades?.length && <EmptyState title="No activity yet" />}
    </div>
  );
}

function DeploymentRow({ deployment, onDelete }: { deployment: PaperDeploymentOut; onDelete: () => void }) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [lastEval, setLastEval] = useState<PaperEvaluationOut | null>(null);

  const evaluateMutation = useMutation({
    mutationFn: () => apiFetch<PaperEvaluationOut>(`/api/v1/paper-trading/deployments/${deployment.id}/evaluate`, { method: "POST" }),
    onSuccess: (data) => {
      setLastEval(data);
      queryClient.invalidateQueries({ queryKey: ["paper-deployments"] });
      queryClient.invalidateQueries({ queryKey: ["paper-portfolio"] });
      queryClient.invalidateQueries({ queryKey: ["paper-orders", deployment.id] });
      queryClient.invalidateQueries({ queryKey: ["paper-trades", deployment.id] });
    },
  });

  const stopMutation = useMutation({
    mutationFn: () => apiFetch(`/api/v1/paper-trading/deployments/${deployment.id}/stop`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["paper-deployments"] }),
  });

  return (
    <>
      <tr className="cursor-pointer hover:bg-surface-elevated" onClick={() => setExpanded(!expanded)}>
        <Td className="font-medium">{deployment.strategy_name}</Td>
        <Td>{deployment.instrument_symbol}</Td>
        <Td>
          <Badge tone={deployment.status === "active" ? "positive" : "inactive"}>{deployment.status}</Badge>
        </Td>
        <Td>
          {deployment.open_position ? (
            <span className="font-financial">
              {deployment.open_position.quantity} @ {deployment.open_position.avg_entry_price.toFixed(2)}
              {deployment.open_position.unrealized_pnl !== null && (
                <span className={deployment.open_position.unrealized_pnl >= 0 ? "text-positive" : "text-negative"}>
                  {" "}
                  ({deployment.open_position.unrealized_pnl >= 0 ? "+" : ""}
                  {deployment.open_position.unrealized_pnl.toFixed(2)})
                </span>
              )}
            </span>
          ) : (
            <span className="text-text-muted">flat</span>
          )}
        </Td>
        <Td className="text-xs text-text-muted">{lastEval ? `${lastEval.action} (${lastEval.signal ?? "-"})` : "--"}</Td>
        <Td className="text-right" onClick={(e) => e.stopPropagation()}>
          <div className="flex justify-end gap-1">
            {deployment.status === "active" ? (
              <>
                <Button variant="ghost" size="sm" onClick={() => evaluateMutation.mutate()} disabled={evaluateMutation.isPending}>
                  <Zap className="h-3.5 w-3.5" /> Evaluate Now
                </Button>
                <Button variant="ghost" size="sm" onClick={() => stopMutation.mutate()} disabled={stopMutation.isPending}>
                  <Square className="h-3.5 w-3.5" /> Stop
                </Button>
              </>
            ) : (
              <Button variant="ghost" size="sm" onClick={onDelete} className="text-text-muted hover:text-negative" title="Delete deployment">
                <Trash2 className="h-3.5 w-3.5" /> Delete
              </Button>
            )}
          </div>
        </Td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={6} className="p-0">
            <DeploymentDetail deployment={deployment} />
          </td>
        </tr>
      )}
    </>
  );
}

export default function PaperTradingPage() {
  const { data: deployments, isLoading } = usePaperDeployments();
  const { data: portfolio } = usePaperPortfolio();
  const [modalOpen, setModalOpen] = useState(false);
  const [toDelete, setToDelete] = useState<PaperDeploymentOut | null>(null);

  return (
    <div className="space-y-6">
      <PaperTradingBanner />

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Paper Trading</h1>
          <p className="text-sm text-text-muted">
            Live Market Data &rarr; Strategy &rarr; Signal &rarr; Risk Engine &rarr; Paper Execution &rarr; Portfolio. Re-evaluated automatically every ~10s, or trigger manually.
          </p>
        </div>
        <Button onClick={() => setModalOpen(true)}>
          <Play className="h-3.5 w-3.5" /> Start Deployment
        </Button>
      </div>

      {portfolio && (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Card>
            <CardContent className="pt-4">
              <div className="text-xs text-text-muted">Equity</div>
              <div className="font-financial text-xl font-semibold text-text-primary">{portfolio.equity.toFixed(2)}</div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <div className="text-xs text-text-muted">Cash</div>
              <div className="font-financial text-xl font-semibold text-text-primary">{portfolio.cash.toFixed(2)}</div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <div className="text-xs text-text-muted">Unrealized P&amp;L</div>
              <div className={`font-financial text-xl font-semibold ${portfolio.unrealized_pnl >= 0 ? "text-positive" : "text-negative"}`}>
                {portfolio.unrealized_pnl.toFixed(2)}
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <div className="text-xs text-text-muted">Realized P&amp;L</div>
              <div className={`font-financial text-xl font-semibold ${portfolio.realized_pnl_total >= 0 ? "text-positive" : "text-negative"}`}>
                {portfolio.realized_pnl_total.toFixed(2)}
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Deployments</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <LoadingState />
          ) : !deployments?.length ? (
            <EmptyState title="No paper trading deployments yet" description="Start a deployment to begin simulated execution." />
          ) : (
            <Table>
              <Thead>
                <tr>
                  <Th>Strategy</Th>
                  <Th>Instrument</Th>
                  <Th>Status</Th>
                  <Th>Position</Th>
                  <Th>Last Signal</Th>
                  <Th />
                </tr>
              </Thead>
              <Tbody>
                {deployments.map((d) => (
                  <DeploymentRow key={d.id} deployment={d} onDelete={() => setToDelete(d)} />
                ))}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>

      <StartDeploymentModal open={modalOpen} onClose={() => setModalOpen(false)} />
      {toDelete && <DeleteDeploymentModal deployment={toDelete} onClose={() => setToDelete(null)} />}
    </div>
  );
}
