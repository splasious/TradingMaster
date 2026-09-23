"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, LogOut, Pencil, Play, Plus, Square, Trash2, Zap } from "lucide-react";
import { useState } from "react";

import { PaperTradingBanner } from "@/components/layout/environment-mode-banner";
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
import {
  useAllNativeTrades,
  useAllPaperTrades,
  useEffectivePcr,
  useInstruments,
  useNativeDeployments,
  useNativeTrades,
  usePaperDeployments,
  usePaperOrders,
  usePaperPortfolios,
  usePaperTrades,
  useStrategies,
} from "@/lib/hooks";
import { marketLabel } from "@/lib/market";
import type {
  InstrumentOut,
  NativeDeploymentOut,
  NativeEvaluationOut,
  NativeHoldingOut,
  NativeLegOut,
  NativePositionOut,
  PaperDeploymentOut,
  PaperEvaluationOut,
  PaperPortfolioOut,
  StrategyOut,
} from "@/lib/types";

function lastEvaluatedDataStatus(lastEvaluatedAt: string | null): DataStatus | undefined {
  if (!lastEvaluatedAt) return undefined;
  const ageSeconds = (Date.now() - new Date(lastEvaluatedAt).getTime()) / 1000;
  return ageSeconds < 60 ? "live" : "stale";
}

function CreatePoolModal({ onClose, onCreated }: { onClose: () => void; onCreated: (portfolioId: string) => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [currency, setCurrency] = useState<"INR" | "USD">("INR");
  const [amount, setAmount] = useState("100000");

  const createMutation = useMutation({
    mutationFn: () =>
      apiFetch<PaperPortfolioOut>("/api/v1/paper-trading/portfolios", {
        method: "POST",
        body: JSON.stringify({ name, currency, initial_capital: Number(amount) }),
      }),
    onSuccess: (pool) => {
      queryClient.invalidateQueries({ queryKey: ["paper-portfolios"] });
      onCreated(pool.id);
    },
  });

  return (
    <Modal open onClose={onClose} title="New Capital Pool">
      <div className="space-y-4">
        <p className="text-sm text-text-secondary">
          A named, currency-scoped pool of capital -- e.g. one INR pool for NSE strategies, one USD pool for Delta
          Exchange strategies. Pools are tracked independently with no currency conversion between them.
        </p>
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Name</label>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Delta USD Pool" />
        </div>
        <div className="flex gap-4">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-text-secondary">Currency</label>
            <Select value={currency} onChange={(e) => setCurrency(e.target.value as "INR" | "USD")} className="w-28">
              <option value="INR">INR</option>
              <option value="USD">USD</option>
            </Select>
          </div>
          <div className="flex-1 space-y-1.5">
            <label className="text-sm font-medium text-text-secondary">Starting Capital</label>
            <Input type="number" min="0.01" step="1" value={amount} onChange={(e) => setAmount(e.target.value)} />
          </div>
        </div>

        {createMutation.error && (
          <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
            {createMutation.error instanceof ApiError ? createMutation.error.message : "Failed to create pool"}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={createMutation.isPending}>
            Cancel
          </Button>
          <Button
            onClick={() => createMutation.mutate()}
            disabled={!name || !amount || Number(amount) <= 0 || createMutation.isPending}
          >
            {createMutation.isPending ? "Creating..." : "Create Pool"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function StartDeploymentModal({ open, onClose, portfolios }: { open: boolean; onClose: () => void; portfolios: PaperPortfolioOut[] }) {
  const queryClient = useQueryClient();
  const { data: allStrategies } = useStrategies();
  // Native (Advanced Python) strategies pick their own instrument(s) live --
  // they don't belong in this single-instrument flow at all. They deploy
  // from the Advanced Strategy Deployments section's own modal below.
  const strategies = allStrategies?.filter((s) => s.code_type !== "native");
  const [strategy, setStrategy] = useState<StrategyOut | null>(null);
  const [instrumentQuery, setInstrumentQuery] = useState("");
  const [instrument, setInstrument] = useState<InstrumentOut | null>(null);
  const { data: rawInstrumentResults } = useInstruments(instrumentQuery);
  // Instruments with no live source backing them are hidden app-wide.
  const instrumentResults = rawInstrumentResults?.filter((i) => i.data_source !== "yahoo_nse" && i.data_source !== "unassigned");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [portfolioId, setPortfolioId] = useState("");
  const [creatingPool, setCreatingPool] = useState(false);

  const strategyInstrumentIds = strategy?.latest_version?.instrument_ids ?? [];
  const usesStrategyInstruments = strategyInstrumentIds.length > 0;

  function reset() {
    setStrategy(null);
    setInstrument(null);
    setInstrumentQuery("");
    setSelectedIds(new Set());
    setPortfolioId("");
  }

  const startMutation = useMutation({
    mutationFn: async () => {
      const targetIds = usesStrategyInstruments ? [...selectedIds] : instrument ? [instrument.id] : [];
      const results = await Promise.allSettled(
        targetIds.map((instrument_id) =>
          apiFetch<PaperDeploymentOut>("/api/v1/paper-trading/deployments", {
            method: "POST",
            body: JSON.stringify({
              strategy_id: strategy!.id,
              instrument_id,
              portfolio_id: portfolioId,
              timeframe: strategy!.latest_version?.timeframe ?? "1d",
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
      queryClient.invalidateQueries({ queryKey: ["paper-deployments"] });
      queryClient.invalidateQueries({ queryKey: ["paper-portfolios"] });
      reset();
      onClose();
    },
  });

  const canStart = (usesStrategyInstruments ? selectedIds.size > 0 : !!instrument) && !!portfolioId;

  return (
    <>
      <Modal
        open={open}
        onClose={() => {
          reset();
          onClose();
        }}
        title="Start Paper Trading"
      >
        <div className="space-y-4">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-text-secondary">Strategy</label>
            <Select
              value={strategy?.id ?? ""}
              onChange={(e) => {
                setStrategy(strategies?.find((s) => s.id === e.target.value) ?? null);
                setInstrument(null);
              }}
            >
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
            <div className="flex items-center justify-between">
              <label className="text-sm font-medium text-text-secondary">Capital Pool</label>
              <button type="button" className="flex items-center gap-1 text-xs text-active hover:underline" onClick={() => setCreatingPool(true)}>
                <Plus className="h-3 w-3" /> New pool
              </button>
            </div>
            <Select value={portfolioId} onChange={(e) => setPortfolioId(e.target.value)}>
              <option value="" disabled>
                Select a capital pool
              </option>
              {portfolios.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.currency} {p.cash.toFixed(0)} available)
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

          {startMutation.error && (
            <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
              {startMutation.error instanceof ApiError ? startMutation.error.message : "Failed to start"}
            </div>
          )}

          <div className="flex justify-end gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                reset();
                onClose();
              }}
            >
              Cancel
            </Button>
            <Button onClick={() => startMutation.mutate()} disabled={!canStart || startMutation.isPending}>
              {startMutation.isPending
                ? "Starting..."
                : usesStrategyInstruments && selectedIds.size > 1
                  ? `Start (${selectedIds.size} instruments)`
                  : "Start"}
            </Button>
          </div>
        </div>
      </Modal>
      {creatingPool && (
        <CreatePoolModal
          onClose={() => setCreatingPool(false)}
          onCreated={(id) => {
            setPortfolioId(id);
            setCreatingPool(false);
          }}
        />
      )}
    </>
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
                <Th className="text-right">Trade Value</Th>
                <Th className="text-right">PnL</Th>
              </tr>
            </Thead>
            <Tbody>
              {trades.map((t) => (
                <tr key={t.id}>
                  <Td className="font-financial text-xs">
                    {new Date(t.entry_ts).toLocaleDateString()} {new Date(t.entry_ts).toLocaleTimeString()}
                    <span className="ml-1.5 text-text-muted">@ {t.entry_price.toFixed(2)}</span>
                  </Td>
                  <Td className="font-financial text-xs">
                    {new Date(t.exit_ts).toLocaleDateString()} {new Date(t.exit_ts).toLocaleTimeString()}
                    <span className="ml-1.5 text-text-muted">@ {t.exit_price.toFixed(2)}</span>
                  </Td>
                  <Td className="text-right font-financial">{t.quantity}</Td>
                  <Td className="text-right font-financial">
                    {(t.quantity * t.entry_price).toLocaleString(undefined, { maximumFractionDigits: 2 })}
                  </Td>
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
      queryClient.invalidateQueries({ queryKey: ["paper-portfolios"] });
      queryClient.invalidateQueries({ queryKey: ["paper-orders", deployment.id] });
      queryClient.invalidateQueries({ queryKey: ["paper-trades", deployment.id] });
    },
  });

  const stopMutation = useMutation({
    mutationFn: () => apiFetch(`/api/v1/paper-trading/deployments/${deployment.id}/stop`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["paper-deployments"] }),
  });

  const exitMutation = useMutation({
    mutationFn: () => apiFetch<PaperEvaluationOut>(`/api/v1/paper-trading/deployments/${deployment.id}/exit`, { method: "POST" }),
    onSuccess: (data) => {
      setLastEval(data);
      queryClient.invalidateQueries({ queryKey: ["paper-deployments"] });
      queryClient.invalidateQueries({ queryKey: ["paper-portfolios"] });
      queryClient.invalidateQueries({ queryKey: ["paper-orders", deployment.id] });
      queryClient.invalidateQueries({ queryKey: ["paper-trades", deployment.id] });
    },
  });

  return (
    <>
      <tr className="cursor-pointer hover:bg-surface-elevated" onClick={() => setExpanded(!expanded)}>
        <Td className="font-medium">{deployment.strategy_name}</Td>
        <Td>{deployment.instrument_symbol}</Td>
        <Td>
          <span className="text-text-secondary">{deployment.portfolio_name}</span>{" "}
          <Badge tone="neutral">{deployment.currency}</Badge>
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
        <Td className="text-text-secondary">
          {deployment.open_position ? (
            <span className="font-financial text-xs" title={new Date(deployment.open_position.opened_at).toISOString()}>
              {new Date(deployment.open_position.opened_at).toLocaleDateString()}{" "}
              {new Date(deployment.open_position.opened_at).toLocaleTimeString()}
            </span>
          ) : (
            <span className="text-text-muted">--</span>
          )}
        </Td>
        <Td className="text-right font-financial">
          {deployment.open_position
            ? (deployment.open_position.quantity * deployment.open_position.avg_entry_price).toLocaleString(undefined, {
                maximumFractionDigits: 2,
              })
            : <span className="text-text-muted">--</span>}
        </Td>
        <Td className="text-right font-financial">
          {deployment.open_position ? (
            <>
              {(
                deployment.open_position.quantity *
                (deployment.open_position.current_price ?? deployment.open_position.avg_entry_price)
              ).toLocaleString(undefined, { maximumFractionDigits: 2 })}
              {deployment.open_position.current_price == null && (
                <span className="ml-1 text-[10px] uppercase text-text-muted">stale</span>
              )}
            </>
          ) : (
            <span className="text-text-muted">--</span>
          )}
        </Td>
        <Td className="text-right font-financial">
          {deployment.open_position && deployment.open_position.unrealized_pnl !== null ? (
            <span className={deployment.open_position.unrealized_pnl >= 0 ? "text-positive" : "text-negative"}>
              {deployment.open_position.unrealized_pnl >= 0 ? "+" : ""}
              {deployment.open_position.unrealized_pnl.toFixed(2)}
            </span>
          ) : (
            <span className="text-text-muted">--</span>
          )}
        </Td>
        <Td className="max-w-xs text-xs text-text-muted">
          {lastEval ? (
            // A just-clicked "Evaluate Now" result, shown immediately --
            // fresher than whatever the next background refetch would
            // otherwise show for a moment.
            <span
              className={`block truncate ${lastEval.action === "error" ? "text-negative" : ""}`}
              title={lastEval.reason ?? undefined}
            >
              {lastEval.action}
              {lastEval.signal ? ` (${lastEval.signal})` : ""}
              {lastEval.reason ? `: ${lastEval.reason}` : ""}
            </span>
          ) : deployment.last_signal ? (
            // The server's own persisted last_signal -- set by the
            // auto-evaluation scheduler on every tick regardless of
            // outcome (paper_trading/engine.py), so this reflects real
            // state without anyone having to click anything.
            <span
              className={`block truncate ${deployment.last_signal === "ERROR" ? "text-negative" : ""}`}
              title={deployment.last_signal_reason ?? undefined}
            >
              {deployment.last_signal}
              {deployment.last_signal_reason ? `: ${deployment.last_signal_reason}` : ""}
            </span>
          ) : (
            "--"
          )}
        </Td>
        <Td className="text-right" onClick={(e) => e.stopPropagation()}>
          <div className="flex justify-end gap-1">
            {deployment.status === "active" ? (
              <>
                <Button variant="ghost" size="sm" onClick={() => evaluateMutation.mutate()} disabled={evaluateMutation.isPending}>
                  <Zap className="h-3.5 w-3.5" /> Evaluate Now
                </Button>
                {deployment.open_position && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => exitMutation.mutate()}
                    disabled={exitMutation.isPending}
                    className="text-negative hover:text-negative"
                    title="Close this position now, at the best available price -- regardless of the strategy's signal"
                  >
                    <LogOut className="h-3.5 w-3.5" /> {exitMutation.isPending ? "Exiting..." : "Exit"}
                  </Button>
                )}
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
          <td colSpan={11} className="p-0">
            <DeploymentDetail deployment={deployment} />
          </td>
        </tr>
      )}
    </>
  );
}

const DEPLOYMENT_TABLE_HEADERS = (
  <tr>
    <Th>Strategy</Th>
    <Th>Instrument</Th>
    <Th>Pool</Th>
    <Th>Status</Th>
    <Th>Position</Th>
    <Th>Entered</Th>
    <Th className="text-right">Trade Value</Th>
    <Th className="text-right">Live Value</Th>
    <Th className="text-right">P&amp;L</Th>
    <Th>Last Signal</Th>
    <Th />
  </tr>
);

function DeploymentsTable({ deployments, onDelete }: { deployments: PaperDeploymentOut[]; onDelete: (d: PaperDeploymentOut) => void }) {
  return (
    <Table>
      <Thead>{DEPLOYMENT_TABLE_HEADERS}</Thead>
      <Tbody>
        {deployments.map((d) => (
          <DeploymentRow key={d.id} deployment={d} onDelete={() => onDelete(d)} />
        ))}
      </Tbody>
    </Table>
  );
}

/** A bulk-deployed strategy (e.g. one rotation basket attached to
 * hundreds of NSE stocks) used to dump every one of those instruments
 * into a single flat table -- 680 rows deep with no way to tell one
 * strategy's block from another's at a glance.
 *
 * Grouped by strategy_name, not strategy_id: a bulk-deploy wizard can
 * create either one Strategy shared across every instrument (one real
 * strategy_id) or a separate Strategy row per instrument that all happen
 * to share the same display name -- grouping by id would silently
 * produce hundreds of one-row "groups" in the latter case, which looks
 * identical to no grouping at all. Grouping by the name the user actually
 * sees collapses correctly either way. */
function groupByStrategy(deployments: PaperDeploymentOut[]): { strategyName: string; deployments: PaperDeploymentOut[] }[] {
  const groups = new Map<string, { strategyName: string; deployments: PaperDeploymentOut[] }>();
  for (const d of deployments) {
    const existing = groups.get(d.strategy_name);
    if (existing) existing.deployments.push(d);
    else groups.set(d.strategy_name, { strategyName: d.strategy_name, deployments: [d] });
  }
  return [...groups.values()].sort((a, b) => a.strategyName.localeCompare(b.strategyName));
}

function StrategyGroup({
  strategyName, deployments, onDelete, defaultOpen = false,
}: {
  strategyName: string; deployments: PaperDeploymentOut[]; onDelete: (d: PaperDeploymentOut) => void; defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-b border-border last:border-b-0">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between px-5 py-3 text-left hover:bg-surface-elevated"
      >
        <span className="flex items-center gap-2 text-sm font-medium text-text-primary">
          {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          {strategyName}
        </span>
        <Badge tone="neutral">{deployments.length}</Badge>
      </button>
      {open && <DeploymentsTable deployments={deployments} onDelete={onDelete} />}
    </div>
  );
}

function GroupedDeploymentsTable({
  deployments, onDelete, defaultOpen = false,
}: {
  deployments: PaperDeploymentOut[]; onDelete: (d: PaperDeploymentOut) => void; defaultOpen?: boolean;
}) {
  const groups = groupByStrategy(deployments);
  // A single strategy's block -- grouping would just add an extra click
  // to see the exact same rows, so skip straight to the flat table.
  if (groups.length <= 1) return <DeploymentsTable deployments={deployments} onDelete={onDelete} />;
  return (
    <div>
      {groups.map((g) => (
        <StrategyGroup key={g.strategyName} strategyName={g.strategyName} deployments={g.deployments} onDelete={onDelete} defaultOpen={defaultOpen} />
      ))}
    </div>
  );
}

/** A stock only counts as "running" while it's actually holding a
 * position -- for a rotation basket like RS Scalper, most of the 30
 * attached instruments sit flat waiting to rank in at any given moment,
 * so filtering by deployment status alone ("active") wouldn't declutter
 * anything. Once a position exits, the deployment naturally falls out of
 * this list (back to flat) and its closed trade becomes a record in the
 * expanded row's Closed Trades table / Reports -- nothing to delete or
 * manage, it just steps down on its own. */
function CollapsibleSection({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  if (count === 0) return null;
  return (
    <Card>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between border-b border-border px-5 py-4 text-left"
      >
        <span className="flex items-center gap-2 text-sm font-semibold text-text-primary">
          {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          {title} ({count})
        </span>
      </button>
      {open && <CardContent className="p-0">{children}</CardContent>}
    </Card>
  );
}

function EditCapitalModal({ portfolio, onClose }: { portfolio: PaperPortfolioOut; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [amount, setAmount] = useState(String(portfolio.initial_capital));

  const updateMutation = useMutation({
    mutationFn: () =>
      apiFetch<PaperPortfolioOut>(`/api/v1/paper-trading/portfolios/${portfolio.id}`, {
        method: "PATCH",
        body: JSON.stringify({ initial_capital: Number(amount) }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["paper-portfolios"] });
      onClose();
    },
  });

  return (
    <Modal open onClose={onClose} title={`Set Capital -- ${portfolio.name}`}>
      <div className="space-y-4">
        <p className="text-sm text-text-secondary">
          Resets both cash and starting capital to this amount. Doesn&apos;t affect existing deployments or trade
          history -- equity just recalculates from the new cash balance.
        </p>
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Amount ({portfolio.currency})</label>
          <Input type="number" min="0.01" step="1" value={amount} onChange={(e) => setAmount(e.target.value)} />
        </div>

        {updateMutation.error && (
          <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
            {updateMutation.error instanceof ApiError ? updateMutation.error.message : "Failed to update capital"}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={() => updateMutation.mutate()}
            disabled={!amount || Number(amount) <= 0 || updateMutation.isPending}
          >
            {updateMutation.isPending ? "Saving..." : "Save"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function DeletePortfolioModal({
  portfolio,
  activeCount,
  inTradeCount,
  onClose,
}: {
  portfolio: PaperPortfolioOut;
  activeCount: number;
  inTradeCount: number;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const deleteMutation = useMutation({
    mutationFn: () => apiFetch(`/api/v1/paper-trading/portfolios/${portfolio.id}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["paper-portfolios"] });
      queryClient.invalidateQueries({ queryKey: ["paper-deployments"] });
      onClose();
    },
  });

  return (
    <Modal open onClose={onClose} title={`Delete Pool: ${portfolio.name}`}>
      <div className="space-y-4">
        <p className="text-sm text-text-secondary">
          Permanently deletes this capital pool along with every deployment in it (and their simulated order/trade
          history) -- including active ones and any open positions, since there&apos;s no real position to unwind in
          paper trading. This cannot be undone.
        </p>
        {activeCount > 0 && (
          <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
            {activeCount} active deployment{activeCount === 1 ? "" : "s"} will be force-stopped and deleted
            {inTradeCount > 0 && `, including ${inTradeCount} currently holding an open position`}.
          </div>
        )}

        {deleteMutation.isError && (
          <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
            {deleteMutation.error instanceof ApiError ? deleteMutation.error.message : "Failed to delete pool"}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={deleteMutation.isPending}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={() => deleteMutation.mutate()} disabled={deleteMutation.isPending}>
            {deleteMutation.isPending ? "Deleting..." : "Delete Pool"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function PortfolioCard({
  portfolio,
  deployments,
  onEdit,
  onDelete,
}: {
  portfolio: PaperPortfolioOut;
  deployments: PaperDeploymentOut[];
  onEdit: () => void;
  onDelete: () => void;
}) {
  // Derived from the same deployments the tables below already render,
  // not the portfolio API's own unrealized_pnl/equity -- those come from a
  // separate request, and with live-streaming prices, two independent
  // snapshots a few seconds apart can legitimately disagree with what's
  // shown per-row. Summing the already-loaded rows guarantees this card
  // always matches what's underneath it.
  const myPositions = deployments
    .filter((d) => d.portfolio_id === portfolio.id && d.open_position)
    .map((d) => d.open_position!);
  const unrealizedPnl = myPositions.reduce((sum, p) => sum + (p.unrealized_pnl ?? 0), 0);

  // Today's Gain = today's realized P&L (both regular and Advanced Python
  // deployments can share a capital pool) + every currently open position's
  // unrealized P&L (regular above, native's own multi-leg one below) --
  // these are intraday ("15 MIN") strategies, so a position still open now
  // was opened today, making this equivalent to today's equity change
  // without needing a start-of-day equity snapshot this app doesn't keep.
  const { data: allTrades } = useAllPaperTrades();
  const { data: allNativeTrades } = useAllNativeTrades();
  const { data: nativeDeployments } = useNativeDeployments();
  const today = new Date().toDateString();
  const myDeploymentIds = new Set(deployments.filter((d) => d.portfolio_id === portfolio.id).map((d) => d.id));
  const myNativeDeploymentIds = new Set((nativeDeployments ?? []).filter((d) => d.portfolio_id === portfolio.id).map((d) => d.id));
  const realizedToday =
    (allTrades ?? []).filter((t) => myDeploymentIds.has(t.deployment_id) && new Date(t.exit_ts).toDateString() === today).reduce((sum, t) => sum + t.pnl, 0) +
    (allNativeTrades ?? [])
      .filter((t) => myNativeDeploymentIds.has(t.deployment_id) && new Date(t.closed_at).toDateString() === today)
      .reduce((sum, t) => sum + t.pnl, 0);
  const nativeUnrealizedPnl = (nativeDeployments ?? [])
    .filter((d) => d.portfolio_id === portfolio.id && d.position)
    .reduce((sum, d) => sum + (d.position!.unrealized_pnl ?? 0), 0);
  // Multi-holding strategies (state["holdings"] -- see NativeDeploymentOut.holdings'
  // docstring) have no single position/unrealized_pnl to read; each holding
  // is its own independent long, so sum (current - entry) * qty per leg.
  const holdingsUnrealizedPnl = (nativeDeployments ?? [])
    .filter((d) => d.portfolio_id === portfolio.id)
    .flatMap((d) => d.holdings ?? [])
    .reduce((sum, l) => sum + (l.current_price != null ? (l.current_price - l.entry_price) * l.quantity : 0), 0);
  const todayGain = realizedToday + unrealizedPnl + nativeUnrealizedPnl + holdingsUnrealizedPnl;
  // Advanced (native) deployments sharing this pool hold capital too -- their
  // legs/holdings count toward equity the same way regular positions do.
  const nativeMarketValue = (nativeDeployments ?? [])
    .filter((d) => d.portfolio_id === portfolio.id)
    .flatMap((d): NativeLegOut[] => (d.position ? d.position.legs : d.holdings ?? []))
    .reduce((sum, l) => sum + legMarketValue(l), 0);
  const equity =
    portfolio.cash + myPositions.reduce((sum, p) => sum + p.quantity * (p.current_price ?? p.avg_entry_price), 0) + nativeMarketValue;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          {portfolio.name} <Badge tone="neutral">{portfolio.currency}</Badge>
        </CardTitle>
        <button onClick={onDelete} className="text-text-muted hover:text-negative" title="Delete this capital pool">
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-4 pt-0 md:grid-cols-5">
        <div>
          <div className="text-xs text-text-muted">Equity</div>
          <div className="font-financial text-lg font-semibold text-text-primary">{equity.toFixed(2)}</div>
        </div>
        <div>
          <div className="flex items-center justify-between">
            <div className="text-xs text-text-muted">Cash</div>
            <button onClick={onEdit} className="text-text-muted hover:text-text-primary" title="Edit starting capital">
              <Pencil className="h-3 w-3" />
            </button>
          </div>
          <div className="font-financial text-lg font-semibold text-text-primary">{portfolio.cash.toFixed(2)}</div>
        </div>
        <div>
          <div className="text-xs text-text-muted">Unrealized P&amp;L</div>
          <div className={`font-financial text-lg font-semibold ${unrealizedPnl >= 0 ? "text-positive" : "text-negative"}`}>
            {unrealizedPnl.toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-xs text-text-muted">Realized P&amp;L</div>
          <div className={`font-financial text-lg font-semibold ${portfolio.realized_pnl_total >= 0 ? "text-positive" : "text-negative"}`}>
            {portfolio.realized_pnl_total.toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-xs text-text-muted">Today&apos;s Gain</div>
          <div className={`font-financial text-lg font-semibold ${todayGain >= 0 ? "text-positive" : "text-negative"}`}>
            {todayGain >= 0 ? "+" : ""}
            {todayGain.toFixed(2)}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function ClosedTradesPanel() {
  const { data: trades, isLoading } = useAllPaperTrades();

  return (
    <CollapsibleSection title="Closed Trades" count={trades?.length ?? 0}>
      {isLoading ? (
        <LoadingState />
      ) : (
        <Table>
          <Thead>
            <tr>
              <Th>Strategy</Th>
              <Th>Instrument</Th>
              <Th>Entered</Th>
              <Th>Exited</Th>
              <Th className="text-right">Qty</Th>
              <Th className="text-right">Trade Value</Th>
              <Th className="text-right">P&amp;L</Th>
              <Th className="text-right">P&amp;L %</Th>
              <Th>Exit Reason</Th>
            </tr>
          </Thead>
          <Tbody>
            {trades?.map((t) => (
              <tr key={t.id}>
                <Td className="font-medium">{t.strategy_name ?? "--"}</Td>
                <Td>{t.instrument_symbol ?? "--"}</Td>
                <Td className="font-financial text-xs">
                  {new Date(t.entry_ts).toLocaleDateString()} {new Date(t.entry_ts).toLocaleTimeString()}
                  <span className="ml-1.5 text-text-muted">@ {t.entry_price.toFixed(2)}</span>
                </Td>
                <Td className="font-financial text-xs">
                  {new Date(t.exit_ts).toLocaleDateString()} {new Date(t.exit_ts).toLocaleTimeString()}
                  <span className="ml-1.5 text-text-muted">@ {t.exit_price.toFixed(2)}</span>
                </Td>
                <Td className="text-right font-financial">{t.quantity}</Td>
                <Td className="text-right font-financial">
                  {(t.quantity * t.entry_price).toLocaleString(undefined, { maximumFractionDigits: 2 })}
                </Td>
                <Td className={`text-right font-financial ${t.pnl >= 0 ? "text-positive" : "text-negative"}`}>
                  {t.pnl >= 0 ? "+" : ""}
                  {t.pnl.toFixed(2)}
                </Td>
                <Td className={`text-right font-financial ${t.pnl_pct >= 0 ? "text-positive" : "text-negative"}`}>
                  {t.pnl_pct >= 0 ? "+" : ""}
                  {t.pnl_pct.toFixed(2)}%
                </Td>
                <Td className="text-text-muted">{t.exit_reason.replace("_", " ")}</Td>
              </tr>
            ))}
          </Tbody>
        </Table>
      )}
    </CollapsibleSection>
  );
}

function NativeClosedTradesPanel() {
  const { data: trades, isLoading } = useAllNativeTrades();

  return (
    <CollapsibleSection title="Advanced Strategy Closed Trades" count={trades?.length ?? 0}>
      {isLoading ? (
        <LoadingState />
      ) : (
        <Table>
          <Thead>
            <tr>
              <Th>Strategy</Th>
              <Th>Opened</Th>
              <Th>Closed</Th>
              <Th className="text-right">P&amp;L</Th>
              <Th className="text-right">P&amp;L %</Th>
              <Th>Exit Reason</Th>
              <Th>Legs</Th>
            </tr>
          </Thead>
          <Tbody>
            {trades?.map((t) => (
              <tr key={t.id}>
                <Td className="font-medium">{t.strategy_name ?? "--"}</Td>
                <Td className="font-financial text-xs">{new Date(t.opened_at).toLocaleDateString()} {new Date(t.opened_at).toLocaleTimeString()}</Td>
                <Td className="font-financial text-xs">{new Date(t.closed_at).toLocaleDateString()} {new Date(t.closed_at).toLocaleTimeString()}</Td>
                <Td className={`text-right font-financial ${t.pnl >= 0 ? "text-positive" : "text-negative"}`}>
                  {t.pnl >= 0 ? "+" : ""}
                  {t.pnl.toFixed(2)}
                </Td>
                <Td className={`text-right font-financial ${t.pnl_pct >= 0 ? "text-positive" : "text-negative"}`}>
                  {t.pnl_pct >= 0 ? "+" : ""}
                  {t.pnl_pct.toFixed(2)}%
                </Td>
                <Td className="text-text-muted">{t.exit_reason.replace("_", " ")}</Td>
                <Td className="text-xs text-text-muted">
                  {t.legs
                    .map((l) => `${l.side} ${l.instrument_symbol ?? "?"} ${l.quantity}@${l.entry_price.toFixed(2)}->${l.exit_price.toFixed(2)}`)
                    .join(", ")}
                </Td>
              </tr>
            ))}
          </Tbody>
        </Table>
      )}
    </CollapsibleSection>
  );
}

function StartNativeDeploymentModal({ open, onClose, portfolios }: { open: boolean; onClose: () => void; portfolios: PaperPortfolioOut[] }) {
  const queryClient = useQueryClient();
  const { data: strategies } = useStrategies();
  const nativeStrategies = strategies?.filter((s) => s.code_type === "native") ?? [];
  const [strategyId, setStrategyId] = useState("");
  const [portfolioId, setPortfolioId] = useState("");

  function reset() {
    setStrategyId("");
    setPortfolioId("");
  }

  const startMutation = useMutation({
    mutationFn: () =>
      apiFetch<NativeDeploymentOut>("/api/v1/paper-trading/native-deployments", {
        method: "POST",
        body: JSON.stringify({ strategy_id: strategyId, portfolio_id: portfolioId }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["native-deployments"] });
      queryClient.invalidateQueries({ queryKey: ["paper-portfolios"] });
      reset();
      onClose();
    },
  });

  return (
    <Modal
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title="Start Advanced Strategy Deployment"
    >
      <div className="space-y-4">
        <p className="text-sm text-text-secondary">
          Advanced Python strategies pick their own instrument(s) live -- no instrument or sizing to choose here, just
          which strategy and which capital pool to run it against.
        </p>
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Strategy</label>
          <Select value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>
            <option value="" disabled>
              Select an Advanced Python strategy
            </option>
            {nativeStrategies.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </Select>
          {!nativeStrategies.length && (
            <p className="text-xs text-text-muted">
              No Advanced Python strategies yet -- create one in the Strategy Builder&apos;s &quot;Advanced
              Python&quot; tab first.
            </p>
          )}
        </div>
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Capital Pool</label>
          <Select value={portfolioId} onChange={(e) => setPortfolioId(e.target.value)}>
            <option value="" disabled>
              Select a capital pool
            </option>
            {portfolios.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.currency} {p.cash.toFixed(0)} available)
              </option>
            ))}
          </Select>
        </div>

        {startMutation.error && (
          <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
            {startMutation.error instanceof ApiError ? startMutation.error.message : "Failed to start"}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button
            variant="secondary"
            onClick={() => {
              reset();
              onClose();
            }}
          >
            Cancel
          </Button>
          <Button onClick={() => startMutation.mutate()} disabled={!strategyId || !portfolioId || startMutation.isPending}>
            {startMutation.isPending ? "Starting..." : "Start"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function legTradeValue(leg: NativeLegOut): number {
  return leg.entry_price * leg.quantity;
}

function legLiveValue(leg: NativeLegOut): number | null {
  return leg.current_price != null ? leg.current_price * leg.quantity : null;
}

function legPnl(leg: NativeLegOut): number | null {
  if (leg.current_price == null) return null;
  const diff = leg.side === "short" ? leg.entry_price - leg.current_price : leg.current_price - leg.entry_price;
  return diff * leg.quantity;
}

function currencySymbol(currency: string): string {
  return currency === "INR" ? "₹" : currency === "USD" ? "$" : "";
}

function formatMoney(value: number, currency: string, signed = false): string {
  const sign = value < 0 ? "-" : signed && value > 0 ? "+" : "";
  return `${sign}${currencySymbol(currency)}${Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function formatPrice(value: number): string {
  return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** A leg's signed contribution to its pool's equity at the live price (entry
 * price until the first tick): a long is an asset worth price x qty, a short
 * a liability of the same -- opening either already moved its entry value
 * through cash, so equity = cash + the sum of these. */
function legMarketValue(leg: NativeLegOut): number {
  const value = (leg.current_price ?? leg.entry_price) * leg.quantity;
  return leg.side === "short" ? -value : value;
}

// Column labels for the per-stock values rotation strategies commonly record
// on each holding; any other key is shown title-cased.
const HOLDING_METRIC_LABELS: Record<string, string> = {
  macd: "MACD",
  macd_hist: "MACD Hist",
  macd_histogram: "MACD Hist",
  macd_signal: "MACD Signal",
  signal: "Signal",
  rsi: "RSI",
  rsi14: "RSI14",
  rsi_14: "RSI14",
  rs_value: "RS",
  score: "Score",
  rank: "Rank",
};
// Known indicators first, in the order a MACD/RSI strategy reasons about
// them; unknown keys alphabetically after; rank always last.
const HOLDING_METRIC_ORDER = ["macd", "macd_hist", "macd_histogram", "macd_signal", "signal", "rsi", "rsi14", "rsi_14", "rs_value", "score"];

const METRIC_ACRONYMS = new Set(["pcr", "rsi", "macd", "atm", "oi", "sma", "ema", "rs", "pnl", "ltp"]);

function metricLabel(key: string): string {
  return (
    HOLDING_METRIC_LABELS[key] ??
    key
      .split("_")
      .map((w) => (METRIC_ACRONYMS.has(w) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1)))
      .join(" ")
  );
}

/** A strategy-recorded value as plain text: ISO dates as "29 Sep 2026",
 * numbers to at most 2 decimals. */
function metricText(value: number | string | boolean | null | undefined): string {
  if (value == null || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return new Date(`${value}T00:00:00`).toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
  }
  return value;
}

function metricColumns(holdings: NativeHoldingOut[]): string[] {
  const keys = new Set(holdings.flatMap((h) => Object.keys(h.metrics ?? {})));
  keys.delete("status"); // shown in the Status column instead
  const position = (k: string) => {
    if (k === "rank") return Number.MAX_SAFE_INTEGER;
    const i = HOLDING_METRIC_ORDER.indexOf(k);
    return i === -1 ? HOLDING_METRIC_ORDER.length : i;
  };
  return [...keys].sort((a, b) => position(a) - position(b) || a.localeCompare(b));
}

function MetricValue({ name, value }: { name: string; value: number | string | boolean | null | undefined }) {
  if (value == null || value === "") return <span className="text-text-muted">—</span>;
  if (typeof value === "boolean") return <>{value ? "Yes" : "No"}</>;
  if (typeof value === "string") return <>{value}</>;
  if (name === "rank") return <>#{value}</>;
  if (name.startsWith("macd")) {
    return (
      <span className={value >= 0 ? "text-positive" : "text-negative"}>
        {value >= 0 ? "+" : ""}
        {value.toFixed(2)}
      </span>
    );
  }
  if (name.startsWith("rsi")) return <>{value.toFixed(1)}</>;
  return <>{value.toLocaleString(undefined, { maximumFractionDigits: 2 })}</>;
}

/** One row per stock a multi-holding (rotation) strategy currently holds:
 * the position itself, its live P&L, and whatever per-stock values the
 * strategy recorded on it (its reasons for holding: MACD, RSI, rank, ...). */
function HoldingsTable({ holdings, currency }: { holdings: NativeHoldingOut[]; currency: string }) {
  const columns = metricColumns(holdings);
  const numericColumns = new Set(
    columns.filter((c) => holdings.every((h) => h.metrics?.[c] == null || typeof h.metrics[c] === "number")),
  );
  const rows = holdings.every((h) => typeof h.metrics?.rank === "number")
    ? [...holdings].sort((a, b) => (a.metrics.rank as number) - (b.metrics.rank as number))
    : holdings;
  const cell = "px-3 py-2 whitespace-nowrap";

  return (
    <Table className="text-xs">
      <Thead>
        <tr>
          <Th className="px-3">Stock</Th>
          <Th className="px-3">Status</Th>
          <Th className="px-3 text-right">Qty</Th>
          <Th className="px-3 text-right">Avg Entry</Th>
          <Th className="px-3 text-right">LTP</Th>
          <Th className="px-3 text-right">Invested</Th>
          <Th className="px-3 text-right">Live Value</Th>
          <Th className="px-3 text-right">P&amp;L {currencySymbol(currency)}</Th>
          <Th className="px-3 text-right">P&amp;L %</Th>
          {columns.map((c) => (
            <Th key={c} className={`px-3 ${numericColumns.has(c) ? "text-right" : ""}`}>
              {metricLabel(c)}
            </Th>
          ))}
          <Th className="px-3">Entry Time</Th>
        </tr>
      </Thead>
      <Tbody>
        {rows.map((h) => {
          const invested = legTradeValue(h);
          const live = legLiveValue(h);
          const pnl = legPnl(h);
          const pnlPct = pnl != null && invested ? (pnl / invested) * 100 : null;
          const pnlTone = pnl == null ? "text-text-muted" : pnl >= 0 ? "text-positive" : "text-negative";
          const status = typeof h.metrics?.status === "string" ? h.metrics.status : "HOLD";
          return (
            <tr key={h.instrument_symbol}>
              <Td className={`${cell} font-medium`}>{h.instrument_symbol}</Td>
              <Td className={cell}>
                <Badge tone="active" className="px-2 py-0.5 text-[10px] uppercase">
                  {status}
                </Badge>
              </Td>
              <Td className={`${cell} text-right font-financial`}>{h.quantity.toLocaleString()}</Td>
              <Td className={`${cell} text-right font-financial`}>{formatPrice(h.entry_price)}</Td>
              <Td className={`${cell} text-right font-financial`}>
                {h.current_price != null ? formatPrice(h.current_price) : <span className="text-text-muted">—</span>}
              </Td>
              <Td className={`${cell} text-right font-financial`}>{formatMoney(invested, currency)}</Td>
              <Td className={`${cell} text-right font-financial`}>
                {live != null ? formatMoney(live, currency) : <span className="text-text-muted">—</span>}
              </Td>
              <Td className={`${cell} text-right font-financial ${pnlTone}`}>{pnl != null ? formatMoney(pnl, currency, true) : "—"}</Td>
              <Td className={`${cell} text-right font-financial ${pnlTone}`}>
                {pnlPct != null ? `${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%` : "—"}
              </Td>
              {columns.map((c) => (
                <Td key={c} className={`${cell} ${numericColumns.has(c) ? "text-right font-financial" : ""}`}>
                  <MetricValue name={c} value={h.metrics?.[c]} />
                </Td>
              ))}
              <Td className={`${cell} text-text-secondary`} title={h.opened_at ?? undefined}>
                {h.opened_at
                  ? new Date(h.opened_at).toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })
                  : "—"}
              </Td>
            </tr>
          );
        })}
      </Tbody>
    </Table>
  );
}

/** One row per leg of an options position (spread, straddle, iron condor). */
function PositionLegsTable({ legs, currency }: { legs: NativeLegOut[]; currency: string }) {
  const cell = "px-3 py-2 whitespace-nowrap";
  return (
    <Table className="text-xs">
      <Thead>
        <tr>
          <Th className="px-3">Side</Th>
          <Th className="px-3">Contract</Th>
          <Th className="px-3 text-right">Strike</Th>
          <Th className="px-3 text-right">Qty</Th>
          <Th className="px-3 text-right">Entry</Th>
          <Th className="px-3 text-right">LTP</Th>
          <Th className="px-3 text-right">Trade Value</Th>
          <Th className="px-3 text-right">Live Value</Th>
          <Th className="px-3 text-right">P&amp;L {currencySymbol(currency)}</Th>
          <Th className="px-3 text-right">P&amp;L %</Th>
        </tr>
      </Thead>
      <Tbody>
        {legs.map((l) => {
          const tradeValue = legTradeValue(l);
          const live = legLiveValue(l);
          const pnl = legPnl(l);
          const pnlPct = pnl != null && tradeValue ? (pnl / tradeValue) * 100 : null;
          const pnlTone = pnl == null ? "text-text-muted" : pnl >= 0 ? "text-positive" : "text-negative";
          return (
            <tr key={l.instrument_symbol}>
              <Td className={cell}>
                <Badge tone={l.side === "short" ? "negative" : "positive"} className="px-2 py-0.5 text-[10px] uppercase">
                  {l.side}
                </Badge>
              </Td>
              <Td className={`${cell} font-medium`}>{l.instrument_symbol}</Td>
              <Td className={`${cell} text-right font-financial`}>
                {l.strike != null ? `${l.strike.toLocaleString()} ${l.option_type ?? ""}` : "—"}
              </Td>
              <Td className={`${cell} text-right font-financial`}>{l.quantity.toLocaleString()}</Td>
              <Td className={`${cell} text-right font-financial`}>{formatPrice(l.entry_price)}</Td>
              <Td className={`${cell} text-right font-financial`}>
                {l.current_price != null ? formatPrice(l.current_price) : <span className="text-text-muted">—</span>}
              </Td>
              <Td className={`${cell} text-right font-financial`}>{formatMoney(tradeValue, currency)}</Td>
              <Td className={`${cell} text-right font-financial`}>
                {live != null ? formatMoney(live, currency) : <span className="text-text-muted">—</span>}
              </Td>
              <Td className={`${cell} text-right font-financial ${pnlTone}`}>{pnl != null ? formatMoney(pnl, currency, true) : "—"}</Td>
              <Td className={`${cell} text-right font-financial ${pnlTone}`}>
                {pnlPct != null ? `${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%` : "—"}
              </Td>
            </tr>
          );
        })}
      </Tbody>
    </Table>
  );
}

// Position fields PositionView lays out itself; any other scalar the
// strategy stores on its position is listed generically after them.
const POSITION_LAYOUT_KEYS = new Set(["pcr_at_entry", "pcr_exit_below", "pcr_exit_above", "entry_spot", "roll_trigger", "expiry", "exit_time"]);

/** An options position (state["position"]): what it is and when it opened,
 * the strategy's own decision inputs next to their live values (PCR at
 * entry vs now and its exit band, entry spot vs now and the roll distance),
 * then one row per leg. */
function PositionView({ deployment, position }: { deployment: NativeDeploymentOut; position: NativePositionOut }) {
  const metrics = position.metrics ?? {};
  const currency = deployment.currency;
  // Same query key PcrTicker already polls, so no extra request.
  const { data: pcrNow } = useEffectivePcr(position.underlying_symbol ?? "NIFTY 50");

  const isCredit = position.trade_value >= 0;
  const pnl = position.unrealized_pnl;
  const pnlPct = pnl != null && position.trade_value ? (pnl / Math.abs(position.trade_value)) * 100 : null;
  const regime = position.bias ?? "in a trade";
  const regimeTone = regime === "bullish" ? "positive" : regime === "bearish" ? "negative" : "neutral";
  const opened = new Date(position.opened_at);
  const openedText =
    opened.toDateString() === new Date().toDateString()
      ? opened.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
      : opened.toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });

  const pcrAtEntry = typeof metrics.pcr_at_entry === "number" ? metrics.pcr_at_entry : null;
  const exitBelow = typeof metrics.pcr_exit_below === "number" ? metrics.pcr_exit_below : null;
  const exitAbove = typeof metrics.pcr_exit_above === "number" ? metrics.pcr_exit_above : null;
  const currentPcr = pcrNow?.pcr ?? null;
  const pcrOutsideBand = currentPcr != null && ((exitBelow != null && currentPcr < exitBelow) || (exitAbove != null && currentPcr > exitAbove));
  const exitRule = [exitBelow != null ? `< ${exitBelow.toFixed(2)}` : null, exitAbove != null ? `> ${exitAbove.toFixed(2)}` : null]
    .filter(Boolean)
    .join(" or ");

  const entrySpot = typeof metrics.entry_spot === "number" ? metrics.entry_spot : null;
  const rollTrigger = typeof metrics.roll_trigger === "number" ? metrics.roll_trigger : null;
  const spotMoved = entrySpot != null && position.underlying_price != null ? position.underlying_price - entrySpot : null;
  const nearRoll = spotMoved != null && rollTrigger != null && Math.abs(spotMoved) >= rollTrigger * 0.8;

  const otherMetrics = Object.entries(metrics).filter(([key]) => !POSITION_LAYOUT_KEYS.has(key));

  return (
    <>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <SummaryField label="Pool">
          <span className="text-text-secondary">{deployment.portfolio_name}</span> <Badge tone="neutral">{currency}</Badge>
        </SummaryField>
        <SummaryField label="Position">
          <Badge tone={regimeTone} className="capitalize">
            {regime}
          </Badge>{" "}
          <span className="text-xs text-text-muted" title={opened.toISOString()}>
            since {openedText}
          </span>
        </SummaryField>
        <SummaryField label={isCredit ? "Net Credit" : "Net Debit"}>{formatMoney(Math.abs(position.trade_value), currency)}</SummaryField>
        <SummaryField label={isCredit ? "Cost to Close" : "Value if Closed"}>
          {position.live_value != null ? formatMoney(Math.abs(position.live_value), currency) : <span className="text-text-muted">--</span>}
        </SummaryField>
        <SummaryField label="Total P&amp;L">
          {pnl != null ? (
            <span className={pnl >= 0 ? "text-positive" : "text-negative"}>
              {formatMoney(pnl, currency, true)}
              {pnlPct != null ? ` (${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%)` : ""}
            </span>
          ) : (
            <span className="text-text-muted">--</span>
          )}
        </SummaryField>
      </div>

      {(pcrAtEntry != null || entrySpot != null || metrics.expiry != null || metrics.exit_time != null || otherMetrics.length > 0) && (
        <div className="grid grid-cols-2 gap-3 rounded-md bg-surface-elevated/50 p-3 sm:grid-cols-5">
          {pcrAtEntry != null && (
            <SummaryField label="PCR (entry → now)">
              {pcrAtEntry.toFixed(3)} →{" "}
              <span className={pcrOutsideBand ? "text-negative" : ""}>{currentPcr != null ? currentPcr.toFixed(3) : "--"}</span>
              {exitRule && <div className="text-xs text-text-muted">exit if {exitRule}</div>}
            </SummaryField>
          )}
          {entrySpot != null && (
            <SummaryField label={`${position.underlying_symbol ?? "Spot"} (entry → now)`}>
              {formatPrice(entrySpot)} → {position.underlying_price != null ? formatPrice(position.underlying_price) : "--"}
              {spotMoved != null && (
                <div className={`text-xs ${nearRoll ? "text-warning" : "text-text-muted"}`}>
                  {spotMoved >= 0 ? "+" : ""}
                  {spotMoved.toFixed(1)} pt{rollTrigger != null ? ` · rolls at ±${rollTrigger}` : ""}
                </div>
              )}
            </SummaryField>
          )}
          {metrics.expiry != null && <SummaryField label="Expiry">{metricText(metrics.expiry)}</SummaryField>}
          {metrics.exit_time != null && <SummaryField label="Hard Exit">{metricText(metrics.exit_time)} IST</SummaryField>}
          {otherMetrics.map(([key, value]) => (
            <SummaryField key={key} label={metricLabel(key)}>
              {metricText(value)}
            </SummaryField>
          ))}
        </div>
      )}

      <div className="rounded-md border border-border">
        <PositionLegsTable legs={position.legs} currency={currency} />
      </div>
    </>
  );
}

function NativeDeploymentDetail({ deployment }: { deployment: NativeDeploymentOut }) {
  const { data: trades } = useNativeTrades(deployment.id);
  const position = deployment.position;
  const hasHoldings = deployment.holdings != null && deployment.holdings.length > 0;

  return (
    <div className="space-y-4 border-t border-border bg-surface-elevated/50 p-4">
      {position ? (
        <div className="text-xs text-text-secondary">
          <span className="font-medium">{position.bias ?? "position"}:</span>{" "}
          {position.legs.map((l) => (
            <span key={l.instrument_symbol} className="mr-3">
              {l.side} {l.quantity} {l.instrument_symbol} @ {l.entry_price.toFixed(2)}
              {l.current_price != null && <> (now {l.current_price.toFixed(2)})</>}
            </span>
          ))}
        </div>
      ) : hasHoldings ? null /* already shown in the card's own HoldingsTable */ : (
        <div className="text-xs text-text-muted">Flat -- no open position.</div>
      )}
      {trades && trades.length > 0 ? (
        <div>
          <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">Closed Trades</h4>
          <Table>
            <Thead>
              <tr>
                <Th>Opened</Th>
                <Th>Closed</Th>
                <Th className="text-right">P&amp;L</Th>
                <Th>Exit Reason</Th>
                <Th>Legs</Th>
              </tr>
            </Thead>
            <Tbody>
              {trades.map((t) => (
                <tr key={t.id}>
                  <Td className="font-financial text-xs">{new Date(t.opened_at).toLocaleString()}</Td>
                  <Td className="font-financial text-xs">{new Date(t.closed_at).toLocaleString()}</Td>
                  <Td className={`text-right font-financial ${t.pnl >= 0 ? "text-positive" : "text-negative"}`}>
                    {t.pnl >= 0 ? "+" : ""}
                    {t.pnl.toFixed(2)}
                  </Td>
                  <Td className="text-text-muted">{t.exit_reason.replace("_", " ")}</Td>
                  <Td className="text-xs text-text-muted">
                    {t.legs
                      .map((l) => `${l.side} ${l.instrument_symbol ?? "?"} ${l.quantity}@${l.entry_price.toFixed(2)}->${l.exit_price.toFixed(2)}`)
                      .join(", ")}
                  </Td>
                </tr>
              ))}
            </Tbody>
          </Table>
        </div>
      ) : (
        <EmptyState title="No closed trades yet" />
      )}
    </div>
  );
}

/** One deployment's own field, shown in its dedicated card's summary grid --
 * matches the label/value pairs the old shared table's columns used to be. */
function SummaryField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-0.5">
      <div className="text-[10px] font-medium uppercase tracking-wide text-text-muted">{label}</div>
      <div className="font-financial text-sm">{children}</div>
    </div>
  );
}

function NativeDeploymentCard({ deployment, soloPortfolio }: { deployment: NativeDeploymentOut; soloPortfolio?: PaperPortfolioOut }) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [lastEval, setLastEval] = useState<NativeEvaluationOut | null>(null);
  const position = deployment.position;
  // Multi-holding strategies (e.g. the MACD/RSI rotation strategy) have no
  // single position -- each holding is its own independently-opened long,
  // so it's rendered the same way as position.legs but without a single
  // bias/opened_at/unrealized_pnl to show.
  const holdings = deployment.holdings;
  const hasHoldings = holdings != null && holdings.length > 0;
  const displayLegs = position ? position.legs : hasHoldings ? holdings : null;
  const holdingsPnl = hasHoldings && holdings.every((l) => l.current_price != null) ? holdings.reduce((sum, l) => sum + legPnl(l)!, 0) : null;

  // Only fetched to compute today's realized P&L for the merged pool-stats
  // row below (soloPortfolio case) -- called unconditionally either way to
  // keep this a plain top-level hook call, per rules of hooks.
  const { data: myTrades } = useNativeTrades(deployment.id);
  const unrealizedForEquity = position ? position.unrealized_pnl ?? 0 : hasHoldings ? holdings.reduce((sum, l) => sum + (legPnl(l) ?? 0), 0) : 0;
  // Cash + what's held is worth now -- not cash + unrealized P&L, which left
  // out the capital sitting in the holdings (a pool with ~20L invested in 5
  // stocks showed Equity as just its spare cash plus the P&L on top).
  const poolEquity = soloPortfolio ? soloPortfolio.cash + (displayLegs ?? []).reduce((sum, l) => sum + legMarketValue(l), 0) : 0;
  const holdingsInvested = hasHoldings ? holdings.reduce((sum, l) => sum + legTradeValue(l), 0) : 0;
  const holdingsLive = hasHoldings && holdings.every((l) => l.current_price != null) ? holdings.reduce((sum, l) => sum + legLiveValue(l)!, 0) : null;
  const today = new Date().toDateString();
  const realizedToday = (myTrades ?? [])
    .filter((t) => new Date(t.closed_at).toDateString() === today)
    .reduce((sum, t) => sum + t.pnl, 0);
  const todayGain = realizedToday + unrealizedForEquity;

  const evaluateMutation = useMutation({
    mutationFn: () => apiFetch<NativeEvaluationOut>(`/api/v1/paper-trading/native-deployments/${deployment.id}/evaluate`, { method: "POST" }),
    onSuccess: (data) => {
      setLastEval(data);
      queryClient.invalidateQueries({ queryKey: ["native-deployments"] });
      queryClient.invalidateQueries({ queryKey: ["paper-portfolios"] });
      queryClient.invalidateQueries({ queryKey: ["native-trades"] });
    },
  });

  const stopMutation = useMutation({
    mutationFn: () => apiFetch(`/api/v1/paper-trading/native-deployments/${deployment.id}/stop`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["native-deployments"] }),
  });

  const restartMutation = useMutation({
    mutationFn: () => apiFetch(`/api/v1/paper-trading/native-deployments/${deployment.id}/restart`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["native-deployments"] }),
  });

  const exitMutation = useMutation({
    mutationFn: () => apiFetch<NativeEvaluationOut>(`/api/v1/paper-trading/native-deployments/${deployment.id}/exit`, { method: "POST" }),
    onSuccess: (data) => {
      setLastEval(data);
      queryClient.invalidateQueries({ queryKey: ["native-deployments"] });
      queryClient.invalidateQueries({ queryKey: ["paper-portfolios"] });
      queryClient.invalidateQueries({ queryKey: ["native-trades"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => apiFetch(`/api/v1/paper-trading/native-deployments/${deployment.id}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["native-deployments"] }),
  });

  const lastSignalText = lastEval ? (
    <span className={lastEval.action === "error" ? "text-negative" : ""} title={lastEval.reason ?? undefined}>
      {lastEval.action}
      {lastEval.signal ? ` (${lastEval.signal})` : ""}
      {lastEval.reason ? `: ${lastEval.reason}` : ""}
    </span>
  ) : deployment.last_signal ? (
    <span
      className={deployment.last_signal === "ERROR" ? "text-negative" : ""}
      title={deployment.last_signal_reason ?? undefined}
    >
      {deployment.last_signal}
      {deployment.last_signal_reason ? `: ${deployment.last_signal_reason}` : ""}
    </span>
  ) : (
    <span className="text-text-muted">--</span>
  );

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <CardTitle>{deployment.strategy_name}</CardTitle>
          <Badge tone={deployment.status === "active" ? "positive" : "inactive"}>{deployment.status}</Badge>
        </div>
        <div className="flex gap-1">
          {deployment.status === "active" ? (
            <>
              <Button variant="ghost" size="sm" onClick={() => evaluateMutation.mutate()} disabled={evaluateMutation.isPending}>
                <Zap className="h-3.5 w-3.5" /> Evaluate Now
              </Button>
              {position != null && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => exitMutation.mutate()}
                  disabled={exitMutation.isPending}
                  className="text-negative hover:text-negative"
                >
                  <LogOut className="h-3.5 w-3.5" /> {exitMutation.isPending ? "Exiting..." : "Exit"}
                </Button>
              )}
              <Button variant="ghost" size="sm" onClick={() => stopMutation.mutate()} disabled={stopMutation.isPending}>
                <Square className="h-3.5 w-3.5" /> Stop
              </Button>
            </>
          ) : (
            <>
              <Button variant="ghost" size="sm" onClick={() => restartMutation.mutate()} disabled={restartMutation.isPending}>
                <Play className="h-3.5 w-3.5" /> {restartMutation.isPending ? "Restarting..." : "Restart"}
              </Button>
              <Button
                variant="ghost" size="sm" onClick={() => deleteMutation.mutate()} disabled={deleteMutation.isPending}
                className="text-text-muted hover:text-negative"
              >
                <Trash2 className="h-3.5 w-3.5" /> Delete
              </Button>
            </>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {soloPortfolio && (
          // This pool backs no other deployment, so its equity/cash/P&L IS
          // this strategy's own -- folded in here instead of a separate
          // PortfolioCard elsewhere on the page (see soloNativePortfolioIds).
          <div className="grid grid-cols-2 gap-4 border-b border-border pb-4 md:grid-cols-5">
            <div>
              <div className="text-xs text-text-muted">Equity</div>
              <div className="font-financial text-lg font-semibold text-text-primary">{poolEquity.toFixed(2)}</div>
            </div>
            <div>
              <div className="text-xs text-text-muted">Cash</div>
              <div className="font-financial text-lg font-semibold text-text-primary">{soloPortfolio.cash.toFixed(2)}</div>
            </div>
            <div>
              <div className="text-xs text-text-muted">Unrealized P&amp;L</div>
              <div className={`font-financial text-lg font-semibold ${unrealizedForEquity >= 0 ? "text-positive" : "text-negative"}`}>
                {unrealizedForEquity.toFixed(2)}
              </div>
            </div>
            <div>
              <div className="text-xs text-text-muted">Realized P&amp;L</div>
              <div className={`font-financial text-lg font-semibold ${soloPortfolio.realized_pnl_total >= 0 ? "text-positive" : "text-negative"}`}>
                {soloPortfolio.realized_pnl_total.toFixed(2)}
              </div>
            </div>
            <div>
              <div className="text-xs text-text-muted">Today&apos;s Gain</div>
              <div className={`font-financial text-lg font-semibold ${todayGain >= 0 ? "text-positive" : "text-negative"}`}>
                {todayGain >= 0 ? "+" : ""}
                {todayGain.toFixed(2)}
              </div>
            </div>
          </div>
        )}
        {position ? (
          <PositionView deployment={deployment} position={position} />
        ) : hasHoldings ? (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
              <SummaryField label="Pool">
                <span className="text-text-secondary">{deployment.portfolio_name}</span> <Badge tone="neutral">{deployment.currency}</Badge>
              </SummaryField>
              <SummaryField label="Position">
                {holdings.length} holding{holdings.length === 1 ? "" : "s"}
              </SummaryField>
              <SummaryField label="Invested">{formatMoney(holdingsInvested, deployment.currency)}</SummaryField>
              <SummaryField label="Live Value">
                {holdingsLive != null ? formatMoney(holdingsLive, deployment.currency) : <span className="text-text-muted">--</span>}
              </SummaryField>
              <SummaryField label="Total P&amp;L">
                {holdingsPnl != null ? (
                  <span className={holdingsPnl >= 0 ? "text-positive" : "text-negative"}>
                    {formatMoney(holdingsPnl, deployment.currency, true)}
                    {holdingsInvested ? ` (${holdingsPnl >= 0 ? "+" : ""}${((holdingsPnl / holdingsInvested) * 100).toFixed(2)}%)` : ""}
                  </span>
                ) : (
                  <span className="text-text-muted">--</span>
                )}
              </SummaryField>
            </div>
            <div className="rounded-md border border-border">
              <HoldingsTable holdings={holdings} currency={deployment.currency} />
            </div>
          </>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            <SummaryField label="Pool">
              <span className="text-text-secondary">{deployment.portfolio_name}</span> <Badge tone="neutral">{deployment.currency}</Badge>
            </SummaryField>
            <SummaryField label="Position">
              <span className="text-text-muted">flat</span>
            </SummaryField>
          </div>
        )}

        <div className="text-xs text-text-muted">
          <span className="font-medium uppercase tracking-wide">Last Signal: </span>
          {lastSignalText}
        </div>

        <button
          onClick={() => setExpanded(!expanded)}
          className="text-xs font-medium text-brand hover:underline"
        >
          {expanded ? "Hide details" : "Show details"}
        </button>
        {expanded && <NativeDeploymentDetail deployment={deployment} />}
      </CardContent>
    </Card>
  );
}

/** Small live-updating badge showing the same PCR number a PCR-driven
 * native strategy decides its bias from -- refetches every 15s. */
function PcrTicker({ underlyingSymbol = "NIFTY 50" }: { underlyingSymbol?: string }) {
  const { data, isLoading } = useEffectivePcr(underlyingSymbol);
  if (isLoading || !data) return null;

  const tone = data.bias === "bullish" ? "positive" : data.bias === "bearish" ? "negative" : "neutral";
  return (
    <>
      <Badge tone="neutral" title={`${underlyingSymbol} live tick`}>
        {underlyingSymbol} {data.spot_price != null ? data.spot_price.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "--"}
      </Badge>
      <Badge tone={tone} title={`${underlyingSymbol} PCR, summed across the nearest ${data.num_expiries} expiries (${data.timeframe})`}>
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
        PCR {data.pcr != null ? data.pcr.toFixed(3) : "--"}
        <span className="capitalize">{data.bias}</span>
      </Badge>
    </>
  );
}

/** Portfolio ids that back exactly one deployment in total (regular +
 * native combined) where that sole deployment is an Advanced (native) one --
 * a dedicated pool-per-strategy pairing, the common case for an Advanced
 * deployment. Its PortfolioCard is folded into that one deployment's own
 * card instead of shown separately (see NativeDeploymentCard's
 * soloPortfolio prop); a pool shared across several deployments, or whose
 * sole deployment is a regular (table-row) one, keeps its own standalone
 * card since there's no single card to fold it into. */
function soloNativePortfolioIds(
  regularDeployments: PaperDeploymentOut[],
  nativeDeployments: NativeDeploymentOut[],
): Set<string> {
  const counts = new Map<string, number>();
  for (const d of regularDeployments) counts.set(d.portfolio_id, (counts.get(d.portfolio_id) ?? 0) + 1);
  for (const d of nativeDeployments) counts.set(d.portfolio_id, (counts.get(d.portfolio_id) ?? 0) + 1);
  return new Set(nativeDeployments.filter((d) => counts.get(d.portfolio_id) === 1).map((d) => d.portfolio_id));
}

function NativeDeploymentsPanel({
  onStart,
  portfolios,
  regularDeployments,
}: {
  onStart: () => void;
  portfolios: PaperPortfolioOut[];
  regularDeployments: PaperDeploymentOut[];
}) {
  const { data: deployments, isLoading } = useNativeDeployments();
  const soloIds = soloNativePortfolioIds(regularDeployments, deployments ?? []);
  const portfolioById = new Map(portfolios.map((p) => [p.id, p]));

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-text-primary">Advanced Strategy Deployments</h2>
          <PcrTicker />
        </div>
        <Button variant="secondary" size="sm" onClick={onStart}>
          <Play className="h-3.5 w-3.5" /> Start Advanced Deployment
        </Button>
      </div>
      {isLoading ? (
        <Card>
          <CardContent>
            <LoadingState />
          </CardContent>
        </Card>
      ) : !deployments?.length ? (
        <Card>
          <CardContent>
            <EmptyState
              title="No Advanced Python deployments yet"
              description="For strategies that pick their own instruments live -- multi-leg options, PCR-driven spreads, and the like."
            />
          </CardContent>
        </Card>
      ) : (
        deployments.map((d) => (
          <NativeDeploymentCard key={d.id} deployment={d} soloPortfolio={soloIds.has(d.portfolio_id) ? portfolioById.get(d.portfolio_id) : undefined} />
        ))
      )}
    </div>
  );
}

export default function PaperTradingPage() {
  const { data: deployments, isLoading } = usePaperDeployments();
  const { data: portfolios } = usePaperPortfolios();
  const { data: nativeDeployments } = useNativeDeployments();
  const standalonePortfolios = (portfolios ?? []).filter(
    (p) => !soloNativePortfolioIds(deployments ?? [], nativeDeployments ?? []).has(p.id),
  );
  const [modalOpen, setModalOpen] = useState(false);
  const [nativeModalOpen, setNativeModalOpen] = useState(false);
  const [creatingPool, setCreatingPool] = useState(false);
  const [toDelete, setToDelete] = useState<PaperDeploymentOut | null>(null);
  const [editingPortfolio, setEditingPortfolio] = useState<PaperPortfolioOut | null>(null);
  const [deletingPortfolio, setDeletingPortfolio] = useState<PaperPortfolioOut | null>(null);

  const inTrade = deployments?.filter((d) => d.status === "active" && d.open_position) ?? [];
  const watching = deployments?.filter((d) => d.status === "active" && !d.open_position) ?? [];
  const stopped = deployments?.filter((d) => d.status === "stopped") ?? [];

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
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => setCreatingPool(true)}>
            <Plus className="h-3.5 w-3.5" /> New Capital Pool
          </Button>
          <Button onClick={() => setModalOpen(true)}>
            <Play className="h-3.5 w-3.5" /> Start Deployment
          </Button>
        </div>
      </div>

      {standalonePortfolios.length > 0 && (
        <div className="space-y-3">
          {standalonePortfolios.map((p) => (
            <PortfolioCard
              key={p.id}
              portfolio={p}
              deployments={deployments ?? []}
              onEdit={() => setEditingPortfolio(p)}
              onDelete={() => setDeletingPortfolio(p)}
            />
          ))}
        </div>
      )}

      <NativeDeploymentsPanel onStart={() => setNativeModalOpen(true)} portfolios={portfolios ?? []} regularDeployments={deployments ?? []} />

      <Card>
        <CardHeader>
          <CardTitle>Running (in a trade)</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <LoadingState />
          ) : !deployments?.length ? (
            <EmptyState title="No paper trading deployments yet" description="Start a deployment to begin simulated execution." />
          ) : !inTrade.length ? (
            <EmptyState
              title="Nothing currently in a trade"
              description="Active deployments are still evaluating in the background -- see Watching below."
            />
          ) : (
            <GroupedDeploymentsTable deployments={inTrade} onDelete={setToDelete} defaultOpen />
          )}
        </CardContent>
      </Card>

      <CollapsibleSection title="Watching (active, not currently in a trade)" count={watching.length}>
        <GroupedDeploymentsTable deployments={watching} onDelete={setToDelete} />
      </CollapsibleSection>

      <CollapsibleSection title="Stopped" count={stopped.length}>
        <GroupedDeploymentsTable deployments={stopped} onDelete={setToDelete} />
      </CollapsibleSection>

      <NativeClosedTradesPanel />

      <ClosedTradesPanel />

      <StartDeploymentModal open={modalOpen} onClose={() => setModalOpen(false)} portfolios={portfolios ?? []} />
      <StartNativeDeploymentModal open={nativeModalOpen} onClose={() => setNativeModalOpen(false)} portfolios={portfolios ?? []} />
      {creatingPool && <CreatePoolModal onClose={() => setCreatingPool(false)} onCreated={() => setCreatingPool(false)} />}
      {toDelete && <DeleteDeploymentModal deployment={toDelete} onClose={() => setToDelete(null)} />}
      {editingPortfolio && <EditCapitalModal portfolio={editingPortfolio} onClose={() => setEditingPortfolio(null)} />}
      {deletingPortfolio && (
        <DeletePortfolioModal
          portfolio={deletingPortfolio}
          activeCount={deployments?.filter((d) => d.portfolio_id === deletingPortfolio.id && d.status === "active").length ?? 0}
          inTradeCount={deployments?.filter((d) => d.portfolio_id === deletingPortfolio.id && d.status === "active" && d.open_position).length ?? 0}
          onClose={() => setDeletingPortfolio(null)}
        />
      )}
    </div>
  );
}
