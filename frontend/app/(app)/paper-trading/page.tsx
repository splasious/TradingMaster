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
  useInstruments,
  usePaperDeployments,
  usePaperOrders,
  usePaperPortfolios,
  usePaperTrades,
  useStrategies,
} from "@/lib/hooks";
import { marketLabel } from "@/lib/market";
import type { InstrumentOut, PaperDeploymentOut, PaperEvaluationOut, PaperPortfolioOut, StrategyOut } from "@/lib/types";

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
  const { data: strategies } = useStrategies();
  const [strategy, setStrategy] = useState<StrategyOut | null>(null);
  const [instrumentQuery, setInstrumentQuery] = useState("");
  const [instrument, setInstrument] = useState<InstrumentOut | null>(null);
  const { data: rawInstrumentResults } = useInstruments(instrumentQuery);
  // NSE/Yahoo is hidden app-wide -- no reachable data source in production.
  const instrumentResults = rawInstrumentResults?.filter((i) => i.data_source !== "yahoo_nse");
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
        <Td className="max-w-xs text-xs text-text-muted">
          {lastEval ? (
            <span
              className={`block truncate ${lastEval.action === "error" ? "text-negative" : ""}`}
              title={lastEval.reason ?? undefined}
            >
              {lastEval.action}
              {lastEval.signal ? ` (${lastEval.signal})` : ""}
              {lastEval.reason ? `: ${lastEval.reason}` : ""}
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
          <td colSpan={7} className="p-0">
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
  onEdit,
  onDelete,
}: {
  portfolio: PaperPortfolioOut;
  onEdit: () => void;
  onDelete: () => void;
}) {
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
      <CardContent className="grid grid-cols-2 gap-4 pt-0 md:grid-cols-4">
        <div>
          <div className="text-xs text-text-muted">Equity</div>
          <div className="font-financial text-lg font-semibold text-text-primary">{portfolio.equity.toFixed(2)}</div>
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
          <div className={`font-financial text-lg font-semibold ${portfolio.unrealized_pnl >= 0 ? "text-positive" : "text-negative"}`}>
            {portfolio.unrealized_pnl.toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-xs text-text-muted">Realized P&amp;L</div>
          <div className={`font-financial text-lg font-semibold ${portfolio.realized_pnl_total >= 0 ? "text-positive" : "text-negative"}`}>
            {portfolio.realized_pnl_total.toFixed(2)}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export default function PaperTradingPage() {
  const { data: deployments, isLoading } = usePaperDeployments();
  const { data: portfolios } = usePaperPortfolios();
  const [modalOpen, setModalOpen] = useState(false);
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

      {portfolios && portfolios.length > 0 && (
        <div className="space-y-3">
          {portfolios.map((p) => (
            <PortfolioCard key={p.id} portfolio={p} onEdit={() => setEditingPortfolio(p)} onDelete={() => setDeletingPortfolio(p)} />
          ))}
        </div>
      )}

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
            <DeploymentsTable deployments={inTrade} onDelete={setToDelete} />
          )}
        </CardContent>
      </Card>

      <CollapsibleSection title="Watching (active, not currently in a trade)" count={watching.length}>
        <DeploymentsTable deployments={watching} onDelete={setToDelete} />
      </CollapsibleSection>

      <CollapsibleSection title="Stopped" count={stopped.length}>
        <DeploymentsTable deployments={stopped} onDelete={setToDelete} />
      </CollapsibleSection>

      <StartDeploymentModal open={modalOpen} onClose={() => setModalOpen(false)} portfolios={portfolios ?? []} />
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
