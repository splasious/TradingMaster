"use client";

/** The Trading page's Paper / Live switch (agreed 2 Oct 2026): a paper
 * strategy card switched to Live on one of the user's tested broker accounts
 * starts a live run beside the paper one (backend: live_trading/live_runs.py,
 * /api/v1/live-native). Nothing is bought at the switch; size is typed in;
 * turning it off with positions open asks each time. */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertOctagon, Pencil, Power } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Select } from "@/components/ui/select";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useBrokerAccounts, useKillSwitch } from "@/lib/hooks";
import { istShortDateTime, istShortTime } from "@/lib/time";
import type { BrokerAccountOut, LiveRunOut } from "@/lib/types";

const MAX_LOTS_PER_LEG = 25; // live_runs.MAX_LOTS_PER_LEG -- under NIFTY's 1,800 freeze quantity

export function brokerText(run: Pick<LiveRunOut, "broker_name" | "account_label">): string {
  return `${run.broker_name} (${run.account_label})`;
}

export function rupees(value: number, signed = false): string {
  const sign = value < 0 ? "-" : signed && value > 0 ? "+" : "";
  return `${sign}₹${Math.abs(value).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function tone(value: number | null | undefined): string {
  return value == null ? "text-text-muted" : value >= 0 ? "text-positive" : "text-negative";
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

function invalidateLive(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: ["live-runs"] });
  queryClient.invalidateQueries({ queryKey: ["live-native-trades"] });
  queryClient.invalidateQueries({ queryKey: ["native-deployments"] });
}

/** PAPER | LIVE -- the switch on a strategy card. */
export function LiveSwitch({ live, onPaper, onLive, disabled }: { live: boolean; onPaper: () => void; onLive: () => void; disabled?: boolean }) {
  const base = "rounded px-2.5 py-1 text-[11px] font-semibold tracking-wide transition-colors disabled:cursor-not-allowed disabled:opacity-50";
  return (
    <div className="inline-flex rounded-md border border-border-strong bg-background p-0.5" role="group" aria-label="Paper or live">
      <button
        type="button"
        aria-pressed={!live}
        disabled={disabled}
        onClick={() => live && onPaper()}
        className={`${base} ${!live ? "bg-warning-soft text-warning" : "text-text-secondary hover:text-text-primary"}`}
      >
        PAPER
      </button>
      <button
        type="button"
        aria-pressed={live}
        disabled={disabled}
        onClick={() => !live && onLive()}
        className={`${base} ${live ? "bg-negative text-white" : "text-text-secondary hover:text-text-primary"}`}
      >
        LIVE
      </button>
    </div>
  );
}

/** Why an account can't take a live strategy, or null when it can. */
function accountProblem(account: BrokerAccountOut): string | null {
  if (!account.broker.supports_live_strategies) return `Live strategies can't trade through ${account.broker.name} yet`;
  if (!account.is_active) return "Turned off in Settings > Brokers";
  if (!account.live_verified_at) return "Not tested yet -- run Test in Settings > Brokers first";
  return null;
}

function connectionText(account: BrokerAccountOut): string {
  return account.connection_status === "connected" ? "logged in" : "log in before 09:15";
}

interface SizeFields {
  lots: string;
  capital: string;
  lossLimit: string;
  product: "overnight" | "intraday";
}

function SizeInputs({ value, onChange }: { value: SizeFields; onChange: (v: SizeFields) => void }) {
  const capital = Number(value.capital);
  const lots = Number(value.lots);
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <div className="space-y-1">
        <label htmlFor="live-lots" className="text-xs font-semibold text-text-secondary">Lots per leg</label>
        <Input id="live-lots" type="number" min={1} max={MAX_LOTS_PER_LEG} step={1} placeholder="Type lots" value={value.lots}
               onChange={(e) => onChange({ ...value, lots: e.target.value })} />
        <p className={`text-xs ${value.lots && (lots < 1 || lots > MAX_LOTS_PER_LEG || !Number.isInteger(lots)) ? "text-negative" : "text-text-muted"}`}>
          Options and futures, 1 to {MAX_LOTS_PER_LEG}. Stocks size from the capital.
        </p>
      </div>
      <div className="space-y-1">
        <label htmlFor="live-capital" className="text-xs font-semibold text-text-secondary">Capital allocation (₹)</label>
        <Input id="live-capital" type="number" min={0} step={10000} placeholder="Type amount" value={value.capital}
               onChange={(e) => onChange({ ...value, capital: e.target.value })} />
        <p className="text-xs text-text-muted">It never uses more than this.</p>
      </div>
      <div className="space-y-1">
        <label htmlFor="live-loss" className="text-xs font-semibold text-text-secondary">Daily loss limit (₹)</label>
        <Input id="live-loss" type="number" min={0} step={500}
               placeholder={capital > 0 ? `3% of capital = ${rupees(capital * 0.03)}` : "Leave empty for 3% of capital"}
               value={value.lossLimit} onChange={(e) => onChange({ ...value, lossLimit: e.target.value })} />
        <p className="text-xs text-text-muted">Reached: it closes everything and pauses until the next session.</p>
      </div>
      <div className="space-y-1">
        <label htmlFor="live-product" className="text-xs font-semibold text-text-secondary">Product</label>
        <Select id="live-product" value={value.product} onChange={(e) => onChange({ ...value, product: e.target.value as SizeFields["product"] })}>
          <option value="overnight">Overnight (NRML / CNC)</option>
          <option value="intraday">Intraday (MIS)</option>
        </Select>
        <p className="text-xs text-text-muted">
          {value.product === "intraday" ? "The broker closes MIS positions itself near 15:20 -- only for strategies that close by then." : "For strategies that hold overnight."}
        </p>
      </div>
    </div>
  );
}

function sizeValid(v: SizeFields): boolean {
  const lots = Number(v.lots);
  return Number.isInteger(lots) && lots >= 1 && lots <= MAX_LOTS_PER_LEG && Number(v.capital) > 0 && (v.lossLimit === "" || Number(v.lossLimit) > 0);
}

function sizeBody(v: SizeFields) {
  return {
    lots_per_leg: Number(v.lots), capital: Number(v.capital), daily_loss_limit: v.lossLimit === "" ? null : Number(v.lossLimit),
    product_style: v.product,
  };
}

export function GoLiveModal({ paperDeploymentId, strategyName, onClose }: { paperDeploymentId: string; strategyName: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data: accounts } = useBrokerAccounts();
  const live = (accounts ?? []).filter((a) => a.environment === "live");
  const firstGood = live.find((a) => !accountProblem(a));
  const [accountId, setAccountId] = useState<string>("");
  const chosen = live.find((a) => a.id === (accountId || firstGood?.id));
  const [size, setSize] = useState<SizeFields>({ lots: "", capital: "", lossLimit: "", product: "overnight" });
  const [understood, setUnderstood] = useState(false);

  const start = useMutation({
    mutationFn: () =>
      apiFetch<LiveRunOut>("/api/v1/live-native/runs", {
        method: "POST",
        body: JSON.stringify({ paper_deployment_id: paperDeploymentId, broker_account_id: chosen!.id, confirmed: true, ...sizeBody(size) }),
      }),
    onSuccess: () => {
      invalidateLive(queryClient);
      onClose();
    },
  });

  return (
    <Modal open onClose={onClose} title={`Go live: ${strategyName}`} className="max-w-lg">
      <div className="space-y-4 text-sm">
        <div className="rounded-md bg-negative-soft px-3 py-2 text-xs font-medium text-negative">
          Real orders on your broker account with real money. Each order is a limit 1% past the live price.
        </div>

        <fieldset className="space-y-1.5">
          <legend className="mb-1.5 text-xs font-semibold text-text-secondary">Broker account</legend>
          {!live.length && (
            <p className="text-xs text-text-muted">
              No live broker account yet -- add one in <Link href="/settings/brokers" className="text-active hover:underline">Settings &gt; Brokers</Link>.
            </p>
          )}
          {live.map((a) => {
            const problem = accountProblem(a);
            const checked = chosen?.id === a.id;
            return (
              <label
                key={a.id}
                className={`flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2 ${
                  problem ? "cursor-not-allowed border-border opacity-55" : checked ? "border-negative bg-negative-soft/40" : "border-border"
                }`}
              >
                <input type="radio" name="live-account" className="mt-1" disabled={!!problem} checked={checked}
                       onChange={() => setAccountId(a.id)} />
                <span>
                  <span className="block font-medium">{a.broker.name} · {a.account_label}</span>
                  <span className="block text-xs text-text-muted">
                    {problem ?? `Tested ${istShortDateTime(a.live_verified_at!)} · ${connectionText(a)}`}
                  </span>
                </span>
              </label>
            );
          })}
        </fieldset>

        <SizeInputs value={size} onChange={setSize} />

        <div className="space-y-1 rounded-md bg-surface-elevated px-3 py-2 text-xs text-text-secondary">
          <p>
            <strong className="text-text-primary">What happens next:</strong> nothing is bought now. The live run starts with no
            positions and acts on the strategy&apos;s next signal, the way a freshly started paper run does.
          </p>
          <p>Paper keeps running beside it at its own size. You can change lots and capital later with Edit on the card.</p>
        </div>

        <label className="flex items-start gap-2 text-xs">
          <input type="checkbox" className="mt-0.5" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />
          <span>I understand this trades real money on {chosen ? `${chosen.broker.name} (${chosen.account_label})` : "the account above"}.</span>
        </label>

        {start.error && <div className="rounded-md bg-negative-soft px-3 py-2 text-negative">{errorText(start.error, "Couldn't go live")}</div>}

        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={start.isPending}>Cancel</Button>
          <Button variant="destructive" onClick={() => start.mutate()} disabled={!chosen || !!accountProblem(chosen) || !sizeValid(size) || !understood || start.isPending}>
            {start.isPending ? "Going live..." : `Go live on ${chosen?.broker.name ?? "broker"}`}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export function EditLiveModal({ run, onClose }: { run: LiveRunOut; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [size, setSize] = useState<SizeFields>({
    lots: String(run.lots_per_leg), capital: String(run.capital), lossLimit: run.daily_loss_limit != null ? String(run.daily_loss_limit) : "",
    product: run.product_style,
  });
  const save = useMutation({
    mutationFn: () => apiFetch<LiveRunOut>(`/api/v1/live-native/runs/${run.id}`, { method: "PATCH", body: JSON.stringify(sizeBody(size)) }),
    onSuccess: () => {
      invalidateLive(queryClient);
      onClose();
    },
  });
  return (
    <Modal open onClose={onClose} title={`Edit live size · ${brokerText(run)}`} className="max-w-lg">
      <div className="space-y-4 text-sm">
        <SizeInputs value={size} onChange={setSize} />
        <p className="text-xs text-text-muted">
          A change applies from the next entry. A position already open keeps its size and closes at that size.
          {run.positions.length > 0 && " The product can only change while it holds nothing."}
        </p>
        {save.error && <div className="rounded-md bg-negative-soft px-3 py-2 text-negative">{errorText(save.error, "Couldn't save")}</div>}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>Cancel</Button>
          <Button onClick={() => save.mutate()} disabled={!sizeValid(size) || save.isPending}>{save.isPending ? "Saving..." : "Save"}</Button>
        </div>
      </div>
    </Modal>
  );
}

export function LiveOffModal({ run, strategyName, onClose }: { run: LiveRunOut; strategyName: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [left, setLeft] = useState<string[] | null>(null);
  const stop = useMutation({
    mutationFn: (closePositions: boolean) =>
      apiFetch<{ left_in_account: string[] }>(`/api/v1/live-native/runs/${run.id}/stop`, {
        method: "POST",
        body: JSON.stringify({ close_positions: closePositions }),
      }),
    onSuccess: (out) => {
      invalidateLive(queryClient);
      if (out.left_in_account.length) setLeft(out.left_in_account);
      else onClose();
    },
    onError: () => invalidateLive(queryClient),
  });
  const held = run.positions.length;
  const choice = "w-full rounded-md border border-border-strong px-3 py-2 text-left hover:border-text-secondary disabled:opacity-50";

  return (
    <Modal open onClose={onClose} title={`Turn live off: ${strategyName}`}>
      <div className="space-y-4 text-sm">
        {left ? (
          <>
            <p className="text-text-secondary">Live is off. Left in your {run.broker_name} account for you to manage:</p>
            <ul className="list-disc pl-5 font-financial text-xs">{left.map((l) => <li key={l}>{l}</li>)}</ul>
            <div className="flex justify-end"><Button onClick={onClose}>Done</Button></div>
          </>
        ) : held ? (
          <>
            <p className="text-text-secondary">
              It holds {held} live position{held === 1 ? "" : "s"} on {brokerText(run)}. What should happen to {held === 1 ? "it" : "them"}?
            </p>
            <div className="space-y-2">
              <button type="button" className={choice} disabled={stop.isPending} onClick={() => stop.mutate(true)}>
                <span className="block font-medium">Close {held === 1 ? "it" : "them"} now</span>
                <span className="block text-xs text-text-muted">Protected limit orders, 1% past the live price. Then paper only.</span>
              </button>
              <button type="button" className={choice} disabled={stop.isPending} onClick={() => stop.mutate(false)}>
                <span className="block font-medium">Leave {held === 1 ? "it" : "them"} in my broker account</span>
                <span className="block text-xs text-text-muted">The app stops managing {held === 1 ? "it" : "them"}. You close {held === 1 ? "it" : "them"} yourself.</span>
              </button>
            </div>
            {stop.error && <div className="rounded-md bg-negative-soft px-3 py-2 text-negative">{errorText(stop.error, "Couldn't turn live off")}</div>}
            <div className="flex justify-end"><Button variant="secondary" onClick={onClose} disabled={stop.isPending}>Keep it live</Button></div>
          </>
        ) : (
          <>
            <p className="text-text-secondary">It holds nothing live. Turning live off leaves the paper run as it is.</p>
            {stop.error && <div className="rounded-md bg-negative-soft px-3 py-2 text-negative">{errorText(stop.error, "Couldn't turn live off")}</div>}
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={onClose} disabled={stop.isPending}>Keep it live</Button>
              <Button variant="destructive" onClick={() => stop.mutate(false)} disabled={stop.isPending}>
                {stop.isPending ? "Turning off..." : "Turn live off"}
              </Button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

/** The button that fixes what paused a live run. */
function FixIt({ run, onEdit }: { run: LiveRunOut; onEdit: () => void }) {
  const queryClient = useQueryClient();
  const { hasRole } = useAuth();
  const resume = useMutation({
    mutationFn: () => apiFetch<LiveRunOut>(`/api/v1/live-native/runs/${run.id}/resume`, { method: "POST" }),
    onSuccess: () => invalidateLive(queryClient),
  });
  const killOff = useMutation({
    mutationFn: () => apiFetch("/api/v1/live-trading/kill-switch/deactivate", { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["kill-switch"] });
      invalidateLive(queryClient);
    },
  });
  const resumeButton = (label = "Resume") => (
    <Button size="sm" variant="secondary" onClick={() => resume.mutate()} disabled={resume.isPending}>
      {resume.isPending ? "Resuming..." : label}
    </Button>
  );
  let action: React.ReactNode = null;
  switch (run.fix) {
    case "login":
      action = (
        <>
          <Link href="/settings/brokers"><Button size="sm" variant="secondary">Log in to {run.broker_name}</Button></Link>
          {resumeButton("Resume after logging in")}
        </>
      );
      break;
    case "test":
      action = <Link href="/settings/brokers"><Button size="sm" variant="secondary">Run broker test</Button></Link>;
      break;
    case "loss_limit":
      action = (
        <>
          <Button size="sm" variant="secondary" onClick={onEdit}>Raise loss limit</Button>
          {resumeButton()}
        </>
      );
      break;
    case "kill_switch":
      action = hasRole("administrator") ? (
        <Button size="sm" variant="secondary" onClick={() => killOff.mutate()} disabled={killOff.isPending}>Turn kill switch off</Button>
      ) : (
        <span className="text-xs">An administrator has to turn the kill switch off.</span>
      );
      break;
    case "resume":
      action = resumeButton();
      break;
    case "check_broker":
      action = resumeButton("Resume after checking the broker");
      break;
  }
  if (!action) return null;
  return (
    <span className="flex flex-wrap items-center gap-2">
      {action}
      {resume.error && <span className="text-xs text-negative">{errorText(resume.error, "Couldn't resume")}</span>}
    </span>
  );
}

function LiveMetric({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 space-y-0.5">
      <div className="text-[10px] font-medium uppercase tracking-wide text-text-muted">{label}</div>
      <div className="font-financial text-sm">{children}</div>
    </div>
  );
}

/** A card's live run: broker, today's live P&L against its loss limit,
 * orders today, size, what paused it and the fix, and what it holds. */
export function LiveRunSection({ run, strategyName, canManage }: { run: LiveRunOut; strategyName: string; canManage: boolean }) {
  const [editing, setEditing] = useState(false);
  const [turningOff, setTurningOff] = useState(false);
  const { data: killSwitch } = useKillSwitch();
  const used = Math.max(0, -run.day_pnl);
  const usedPct = run.loss_limit > 0 ? Math.min(100, (used / run.loss_limit) * 100) : 0;
  const paused = run.status === "paused";
  const blocked = !!killSwitch?.active;

  return (
    <section className="space-y-3 rounded-md border border-negative/40 bg-negative-soft/20 p-3" aria-label="Live run">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="negative" className="px-2 py-0.5 text-[11px] font-semibold">LIVE · {brokerText(run)}</Badge>
          {paused && <Badge tone="inactive" className="px-2 py-0.5 text-[11px]">Paused</Badge>}
          {!paused && blocked && <Badge tone="critical" className="px-2 py-0.5 text-[11px]">Kill switch on</Badge>}
          <span className="text-xs text-text-muted" title="When it went live">since {istShortDateTime(run.created_at)}</span>
        </div>
        {canManage && (
          <div className="flex gap-1">
            <Button variant="ghost" size="sm" onClick={() => setEditing(true)}><Pencil className="h-3.5 w-3.5" /> Edit</Button>
            <Button variant="ghost" size="sm" className="text-negative hover:text-negative" onClick={() => setTurningOff(true)}>
              <Power className="h-3.5 w-3.5" /> Turn live off
            </Button>
          </div>
        )}
      </div>

      {(paused || blocked) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md bg-warning-soft px-3 py-2 text-xs text-warning">
          <span className="min-w-0 flex-1 basis-60">
            {paused ? `Paused: ${run.pause_reason ?? "no reason recorded"}` : `Kill switch is on: no live orders. ${killSwitch?.reason ?? ""}`}
            {paused && run.resume_at ? ` Resumes by itself at ${istShortDateTime(run.resume_at)}.` : ""}
          </span>
          {canManage && <FixIt run={run} onEdit={() => setEditing(true)} />}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <LiveMetric label="Live today"><span className={tone(run.day_pnl)}>{rupees(run.day_pnl, true)}</span></LiveMetric>
        <LiveMetric label="Loss limit used">
          {rupees(used)} of {rupees(run.loss_limit)}
          <span className="mt-1 block h-1.5 overflow-hidden rounded bg-neutral-soft">
            <span className={`block h-full rounded ${usedPct >= 80 ? "bg-negative" : "bg-warning"}`} style={{ width: `${usedPct}%` }} />
          </span>
        </LiveMetric>
        <LiveMetric label="Orders today">{run.orders_today} of {run.max_orders_per_day}</LiveMetric>
        <LiveMetric label="Size">{run.lots_per_leg} lot{run.lots_per_leg === 1 ? "" : "s"}/leg · {rupees(run.capital)}</LiveMetric>
        <LiveMetric label="Product">{run.product_style === "intraday" ? "Intraday (MIS)" : "Overnight"}</LiveMetric>
      </div>

      {run.positions.length > 0 ? (
        <div className="overflow-x-auto rounded-md border border-border bg-surface">
          <Table className="text-xs">
            <Thead>
              <tr>
                <Th className="px-3">Side</Th>
                <Th className="px-3">Contract</Th>
                <Th className="px-3 text-right">Qty (lots)</Th>
                <Th className="px-3 text-right">Avg fill</Th>
                <Th className="px-3 text-right">LTP</Th>
                <Th className="px-3 text-right">P&amp;L ₹</Th>
                <Th className="px-3">Since</Th>
              </tr>
            </Thead>
            <Tbody>
              {run.positions.map((p) => (
                <tr key={p.instrument_symbol}>
                  <Td className="px-3 py-2">
                    <Badge tone={p.side === "short" ? "negative" : "positive"} className="px-2 py-0.5 text-[10px] uppercase">{p.side}</Badge>
                  </Td>
                  <Td className="whitespace-nowrap px-3 py-2 font-medium">{p.instrument_symbol}</Td>
                  <Td className="whitespace-nowrap px-3 py-2 text-right font-financial">
                    {p.quantity.toLocaleString("en-IN")}{p.lots != null ? ` (${Number.isInteger(p.lots) ? p.lots : p.lots.toFixed(2)})` : ""}
                  </Td>
                  <Td className="px-3 py-2 text-right font-financial">{p.avg_price.toFixed(2)}</Td>
                  <Td className="px-3 py-2 text-right font-financial">{p.current_price != null ? p.current_price.toFixed(2) : "—"}</Td>
                  <Td className={`px-3 py-2 text-right font-financial ${tone(p.pnl)}`}>{p.pnl != null ? rupees(p.pnl, true) : "—"}</Td>
                  <Td className="whitespace-nowrap px-3 py-2 text-text-secondary">{p.opened_at ? istShortTime(p.opened_at) : "—"}</Td>
                </tr>
              ))}
            </Tbody>
          </Table>
        </div>
      ) : (
        <p className="text-xs text-text-muted">Holds nothing live yet -- it acts on the strategy&apos;s next signal.</p>
      )}

      {run.last_signal && (
        <p className="truncate text-xs text-text-muted" title={run.last_signal_reason ?? undefined}>
          <span className="font-medium uppercase tracking-wide">Last live check: </span>
          {run.last_signal}{run.last_signal_reason ? `: ${run.last_signal_reason}` : ""}
          {run.last_evaluated_at ? ` · ${istShortTime(run.last_evaluated_at)}` : ""}
        </p>
      )}

      {editing && <EditLiveModal run={run} onClose={() => setEditing(false)} />}
      {turningOff && <LiveOffModal run={run} strategyName={strategyName} onClose={() => setTurningOff(false)} />}
    </section>
  );
}

/** The red bar at the top of the Trading page while anything is live: which
 * brokers, Exit all live positions, and the kill switch. */
export function LiveBar({ runs }: { runs: LiveRunOut[] }) {
  const queryClient = useQueryClient();
  const { hasRole } = useAuth();
  const { data: killSwitch } = useKillSwitch();
  const [confirmExit, setConfirmExit] = useState(false);
  const [killReason, setKillReason] = useState("");
  const [killOpen, setKillOpen] = useState(false);
  const exitAll = useMutation({
    mutationFn: () => apiFetch<{ paused: number; not_closed: string[] }>("/api/v1/live-native/exit-all", { method: "POST" }),
    onSuccess: () => invalidateLive(queryClient),
  });
  const kill = useMutation({
    mutationFn: (on: boolean) =>
      on
        ? apiFetch("/api/v1/live-trading/kill-switch/activate", { method: "POST", body: JSON.stringify({ reason: killReason }) })
        : apiFetch("/api/v1/live-trading/kill-switch/deactivate", { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["kill-switch"] });
      invalidateLive(queryClient);
      setKillOpen(false);
      setKillReason("");
    },
  });
  const brokers = [...new Set(runs.map((r) => r.broker_name))];
  const held = runs.reduce((n, r) => n + r.positions.length, 0);

  return (
    <>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border border-negative/40 bg-negative-soft px-4 py-2 text-sm font-semibold text-negative" role="status">
        <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-negative" aria-hidden />
        <span className="min-w-0 flex-1 basis-56">
          {runs.length} strateg{runs.length === 1 ? "y is" : "ies are"} trading real money: {brokers.join(", ")}
          {killSwitch?.active && <span className="ml-2 rounded bg-negative px-1.5 py-0.5 text-[11px] text-white">KILL SWITCH ON</span>}
        </span>
        <Button size="sm" variant="secondary" onClick={() => setConfirmExit(true)} disabled={!held}>
          Exit all live positions
        </Button>
        {hasRole("administrator") && (
          <Button size="sm" variant="destructive" onClick={() => setKillOpen(true)}>
            <AlertOctagon className="h-3.5 w-3.5" /> {killSwitch?.active ? "Kill switch is on" : "Kill switch"}
          </Button>
        )}
      </div>

      <Modal open={confirmExit} onClose={() => setConfirmExit(false)} title="Exit all live positions?">
        <div className="space-y-4 text-sm">
          {exitAll.data ? (
            <>
              <p className="text-text-secondary">
                {exitAll.data.not_closed.length
                  ? `Couldn't close: ${exitAll.data.not_closed.join(", ")} -- check the broker.`
                  : "Every live position is closed."}{" "}
                {exitAll.data.paused} live run{exitAll.data.paused === 1 ? " is" : "s are"} paused until you resume {exitAll.data.paused === 1 ? "it" : "them"}.
              </p>
              <div className="flex justify-end"><Button onClick={() => { exitAll.reset(); setConfirmExit(false); }}>Done</Button></div>
            </>
          ) : (
            <>
              <p className="text-text-secondary">
                Closes all {held} live position{held === 1 ? "" : "s"} with protected limit orders and pauses every live run. Paper runs
                carry on. Resume each live run from its card when ready.
              </p>
              {exitAll.error && <div className="rounded-md bg-negative-soft px-3 py-2 text-negative">{errorText(exitAll.error, "Couldn't exit")}</div>}
              <div className="flex justify-end gap-2">
                <Button variant="secondary" onClick={() => setConfirmExit(false)} disabled={exitAll.isPending}>Cancel</Button>
                <Button variant="destructive" onClick={() => exitAll.mutate()} disabled={exitAll.isPending}>
                  {exitAll.isPending ? "Closing..." : "Close everything"}
                </Button>
              </div>
            </>
          )}
        </div>
      </Modal>

      <Modal open={killOpen} onClose={() => setKillOpen(false)} title={killSwitch?.active ? "Kill switch is on" : "Turn the kill switch on?"}>
        <div className="space-y-4 text-sm">
          {killSwitch?.active ? (
            <p className="text-text-secondary">No live order can be sent by anyone until it&apos;s turned off. {killSwitch.reason ?? ""}</p>
          ) : (
            <>
              <p className="text-text-secondary">
                Stops every live order for every user straight away. Positions stay as they are -- use Exit all live positions to close yours.
              </p>
              <Input placeholder="Reason" value={killReason} onChange={(e) => setKillReason(e.target.value)} />
            </>
          )}
          {kill.error && <div className="rounded-md bg-negative-soft px-3 py-2 text-negative">{errorText(kill.error, "Couldn't change the kill switch")}</div>}
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setKillOpen(false)}>Cancel</Button>
            {killSwitch?.active ? (
              <Button onClick={() => kill.mutate(false)} disabled={kill.isPending}>Turn it off</Button>
            ) : (
              <Button variant="destructive" onClick={() => kill.mutate(true)} disabled={!killReason || kill.isPending}>Turn it on</Button>
            )}
          </div>
        </div>
      </Modal>
    </>
  );
}
