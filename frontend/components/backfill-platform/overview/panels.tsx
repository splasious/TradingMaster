"use client";

import { Activity, CheckCircle2, Info, Pause, Play, RefreshCw, TriangleAlert, XCircle } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ApiError } from "@/lib/api";
import type { BfAttentionItem, BfQueue, BfSchedule, BfTimeframe } from "@/lib/types";
import { cn } from "@/lib/utils";

import { fmtDay, fmtEta, fmtTime, TIMEFRAME_SHORT } from "./format";
import { FreshBadge } from "./fresh-status";
import { Meter } from "./summary";
import { useBackfillActions } from "./use-backfill-actions";

function PanelHeader({ title, right }: { title: string; right?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 px-4 pb-2 pt-3.5">
      <h3 className="text-sm font-semibold text-text-primary">{title}</h3>
      {right}
    </div>
  );
}

function errorText(error: unknown): string | null {
  if (!error) return null;
  return error instanceof ApiError ? error.message : "Something went wrong";
}

function Stat({ value, label }: { value: number; label: string }) {
  return (
    <div className="rounded-md border border-border px-2.5 py-2">
      <div className="font-financial text-base font-semibold text-text-primary">{value.toLocaleString("en-IN")}</div>
      <div className="text-[11.5px] text-text-muted">{label}</div>
    </div>
  );
}

export function JobQueueCard({ queue, canOperate }: { queue: BfQueue; canOperate: boolean }) {
  const { setPaused, cancelRun } = useBackfillActions();
  const run = queue.run;
  const badge =
    queue.state === "running" ? <FreshBadge kind="updating" label="Running" />
    : queue.state === "paused" ? <FreshBadge kind="paused" label="Paused" />
    : queue.state === "waiting_login" ? <FreshBadge kind="waiting" label="Waiting for login" />
    : <FreshBadge kind="ok" label="Idle" />;
  return (
    <Card>
      <PanelHeader title="Job queue" right={badge} />
      <div className="px-4 pb-4">
        {run ? (
          <>
            <div className="flex items-center justify-between text-[13px] text-text-primary">
              <span>
                {run.kind === "scheduled" ? "Daily top-up" : "Top-up"}
                {run.started_at ? ` · started ${fmtTime(run.started_at)} IST` : ""}
              </span>
              <span className="font-financial">{run.percent}%</span>
            </div>
            <div className="mt-2"><Meter percent={run.percent} /></div>
            <div className="mt-3 grid grid-cols-4 gap-2">
              <Stat value={run.running} label="Running" />
              <Stat value={run.queued} label="Queued" />
              <Stat value={run.done} label="Done" />
              <Stat value={run.failed} label="Failed" />
            </div>
            {fmtEta(run.eta_seconds) && <p className="mt-2 text-xs text-text-muted">{fmtEta(run.eta_seconds)}</p>}
          </>
        ) : (
          <p className="text-[13px] text-text-secondary">
            {queue.state === "waiting_login"
              ? queue.waiting_message ?? "Waiting for Zerodha login"
              : queue.queued_total
                ? `${queue.queued_total.toLocaleString("en-IN")} job${queue.queued_total === 1 ? "" : "s"} queued.`
                : "Nothing queued -- the next top-up adds only what is new."}
          </p>
        )}
        {queue.current && (
          <p className="mt-3 border-t border-border pt-3 text-[12.5px] text-text-secondary">
            Now fetching <b className="font-financial font-semibold text-text-primary">{queue.current.symbol} · {TIMEFRAME_SHORT[queue.current.timeframe] ?? queue.current.timeframe}</b>
            {queue.current.from_date ? ` -- from ${fmtDay(`${queue.current.from_date}T12:00:00+05:30`)}` : ""}
          </p>
        )}
        <p className="mt-2 text-xs text-text-muted">One job at a time within Kite&apos;s 3 requests/s · the queue survives restarts</p>
        {canOperate && (queue.state === "paused" || queue.queued_total > 0) && (
          <div className="mt-3 flex gap-2">
            <Button size="sm" variant="secondary" disabled={setPaused.isPending} onClick={() => setPaused.mutate(queue.state !== "paused")}>
              {queue.state === "paused" ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
              {queue.state === "paused" ? "Resume" : "Pause"}
            </Button>
            {run && (
              <Button size="sm" variant="ghost" disabled={cancelRun.isPending} onClick={() => cancelRun.mutate()}>
                Cancel run
              </Button>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

const SEVERITY = {
  bad: { Icon: XCircle, className: "text-negative" },
  warn: { Icon: TriangleAlert, className: "text-warning" },
  info: { Icon: Info, className: "text-active" },
} as const;

export function AttentionCard({ items, canOperate }: { items: BfAttentionItem[]; canOperate: boolean }) {
  const { topUp, retryFailed } = useBackfillActions();
  const error = errorText(topUp.error ?? retryFailed.error);
  return (
    <Card>
      <PanelHeader title="Needs attention" right={<span className="text-xs text-text-muted">{items.length ? `${items.length} item${items.length === 1 ? "" : "s"}` : ""}</span>} />
      <div className="px-4 pb-3">
        {items.length === 0 && (
          <div className="flex items-center gap-2 py-3 text-[13px] text-positive">
            <CheckCircle2 className="h-4 w-4" /> All clear -- nothing is behind or failing.
          </div>
        )}
        {items.map((item, i) => {
          const { Icon, className } = SEVERITY[item.severity];
          const action = item.action;
          return (
            <div key={i} className="grid grid-cols-[20px_1fr_auto] items-start gap-2.5 border-b border-border py-2.5 last:border-b-0">
              <Icon className={cn("mt-0.5 h-4 w-4", className)} aria-hidden />
              <div>
                <div className="text-[13px] font-medium text-text-primary">{item.title}</div>
                <div className="mt-0.5 text-xs text-text-muted">{item.detail}</div>
              </div>
              {action?.type === "link" && (
                <Link href={action.href} className="rounded-md border border-border px-2.5 py-1 text-xs font-medium text-text-primary hover:border-border-strong">
                  Open
                </Link>
              )}
              {canOperate && action?.type === "topup" && (
                <Button size="sm" variant="secondary" className="h-7 px-2.5 text-xs" disabled={topUp.isPending} onClick={() => topUp.mutate([action.source])}>
                  Top up
                </Button>
              )}
              {canOperate && action?.type === "retry_failed" && (
                <Button size="sm" variant="secondary" className="h-7 px-2.5 text-xs" disabled={retryFailed.isPending} onClick={() => retryFailed.mutate(action.kind)}>
                  Re-run
                </Button>
              )}
            </div>
          );
        })}
        {error && <p className="pt-2 text-xs text-negative">{error}</p>}
        {topUp.data && <p className="pt-2 text-xs text-positive">Queued {Object.values(topUp.data.queued).reduce((a, b) => a + b, 0).toLocaleString("en-IN")} top-up jobs.</p>}
        {retryFailed.data && <p className="pt-2 text-xs text-positive">Re-queued {retryFailed.data.requeued.toLocaleString("en-IN")} jobs.</p>}
      </div>
    </Card>
  );
}

// No 1-minute: it is no longer kept (backend timeframes.py).
const ALL_TIMEFRAMES: BfTimeframe[] = ["5m", "15m", "30m", "60m", "1d"];

function TimeInput({ id, label, value, disabled, onChange }: { id: string; label: string; value: string; disabled: boolean; onChange: (v: string) => void }) {
  return (
    <input
      id={id}
      type="time"
      aria-label={label}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className="h-7 rounded-md border border-border bg-surface px-2 font-financial text-xs text-text-primary disabled:opacity-70 dark:[color-scheme:dark]"
    />
  );
}

function Toggle({ on, onChange, disabled, label }: { on: boolean; onChange: (v: boolean) => void; disabled: boolean; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!on)}
      className={cn(
        "relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition-colors disabled:cursor-default",
        on ? "bg-brand" : "bg-border-strong",
      )}
    >
      <span className={cn("absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all", on ? "right-0.5" : "left-0.5")} />
    </button>
  );
}

export function ScheduleCard({ schedule, canEdit }: { schedule: BfSchedule; canEdit: boolean }) {
  // Remounted (fresh draft) only when the saved schedule itself changes --
  // not on every overview refresh, which would wipe an edit in progress.
  const saved = [
    schedule.auto_topup_zerodha, schedule.auto_topup_zerodha_nfo, schedule.delta_enabled,
    schedule.topup_time, schedule.live_start, schedule.live_end, schedule.topup_timeframes.join(),
  ].join("|");
  return <ScheduleForm key={saved} schedule={schedule} canEdit={canEdit} />;
}

function ScheduleForm({ schedule, canEdit }: { schedule: BfSchedule; canEdit: boolean }) {
  const { saveSchedule } = useBackfillActions();
  const [draft, setDraft] = useState(schedule);
  const dirty =
    draft.auto_topup_zerodha !== schedule.auto_topup_zerodha ||
    draft.auto_topup_zerodha_nfo !== schedule.auto_topup_zerodha_nfo ||
    draft.delta_enabled !== schedule.delta_enabled ||
    draft.topup_time !== schedule.topup_time ||
    draft.live_start !== schedule.live_start ||
    draft.live_end !== schedule.live_end ||
    draft.topup_timeframes.join() !== schedule.topup_timeframes.join();
  const set = (patch: Partial<BfSchedule>) => setDraft((d) => ({ ...d, ...patch }));
  const toggleTf = (tf: BfTimeframe) =>
    set({ topup_timeframes: draft.topup_timeframes.includes(tf) ? draft.topup_timeframes.filter((t) => t !== tf) : ALL_TIMEFRAMES.filter((t) => t === tf || draft.topup_timeframes.includes(t)) });
  const next = schedule.next_run_at ? `next ${fmtDay(schedule.next_run_at)}` : "off";

  const rows = [
    { key: "auto_topup_zerodha" as const, title: "Zerodha NSE", detail: `Every trading day ${draft.topup_time} IST · ${next}` },
    { key: "auto_topup_zerodha_nfo" as const, title: "Zerodha NFO", detail: `${draft.topup_time} IST · active contracts only (not expired)` },
    { key: "delta_enabled" as const, title: "Delta Exchange", detail: draft.delta_enabled ? "Live 1-minute sync on" : "Frozen -- data kept, nothing fetched" },
  ];
  return (
    <Card id="automatic-schedule">
      <PanelHeader title="Automatic schedule" right={<span className="text-xs text-text-muted">IST · NSE trading days</span>} />
      <div className="px-4 pb-4">
        {rows.map((row) => {
          const on = draft[row.key];
          return (
            <div key={row.key} className="grid grid-cols-[36px_1fr_auto] items-start gap-3 border-b border-border py-2.5">
              <Toggle on={on} onChange={(v) => set({ [row.key]: v })} disabled={!canEdit} label={row.title} />
              <div>
                <div className="text-[13px] font-medium text-text-primary">{row.title}</div>
                <div className="text-xs text-text-muted">{row.detail}</div>
              </div>
              {row.key === "delta_enabled" && !on ? <FreshBadge kind="paused" label="Paused" /> : on ? <FreshBadge kind="ok" label="On" /> : <FreshBadge kind="none" label="Off" />}
            </div>
          );
        })}
        <div className="grid grid-cols-[36px_1fr_auto] items-start gap-3 border-b border-border py-2.5">
          <Activity className="mx-auto mt-0.5 h-4 w-4 text-text-muted" aria-hidden />
          <div>
            <div className="text-[13px] font-medium text-text-primary">Live data</div>
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-text-muted">
              <TimeInput id="live-start" label="Live data starts" value={draft.live_start} disabled={!canEdit} onChange={(v) => set({ live_start: v })} />
              to
              <TimeInput id="live-end" label="Live data ends" value={draft.live_end} disabled={!canEdit} onChange={(v) => set({ live_end: v })} />
              IST · trading days
            </div>
          </div>
          <FreshBadge kind="ok" label="On" />
        </div>
        <div className="flex flex-wrap items-center gap-2 py-2.5">
          <label className="text-xs text-text-muted" htmlFor="topup-time">Top-up at</label>
          <TimeInput id="topup-time" label="Top-up time" value={draft.topup_time} disabled={!canEdit} onChange={(v) => set({ topup_time: v })} />
          <span className="ml-1 text-xs text-text-muted">Timeframes</span>
          {ALL_TIMEFRAMES.map((tf) => {
            const on = draft.topup_timeframes.includes(tf);
            return (
              <button
                key={tf}
                type="button"
                disabled={!canEdit}
                aria-pressed={on}
                onClick={() => toggleTf(tf)}
                className={cn(
                  "rounded-full border px-2.5 py-0.5 text-xs",
                  on ? "border-text-primary bg-text-primary text-surface" : "border-border text-text-secondary",
                )}
              >
                {TIMEFRAME_SHORT[tf]}
              </button>
            );
          })}
        </div>
        <p className="text-[11.5px] text-text-muted">Skips NSE holidays · waits for the Zerodha login · a missed day is caught up by the next run</p>
        {canEdit && dirty && (
          <div className="mt-3 flex items-center gap-2">
            <Button
              size="sm"
              disabled={saveSchedule.isPending || draft.topup_timeframes.length === 0 || draft.live_start >= draft.live_end}
              onClick={() =>
                saveSchedule.mutate({
                  auto_topup_zerodha: draft.auto_topup_zerodha,
                  auto_topup_zerodha_nfo: draft.auto_topup_zerodha_nfo,
                  delta_enabled: draft.delta_enabled,
                  topup_time: draft.topup_time,
                  live_start: draft.live_start,
                  live_end: draft.live_end,
                  topup_timeframes: draft.topup_timeframes,
                })
              }
            >
              {saveSchedule.isPending && <RefreshCw className="h-3.5 w-3.5 animate-spin" />}Save schedule
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setDraft(schedule)}>Discard</Button>
          </div>
        )}
        {canEdit && draft.live_start >= draft.live_end && <p className="mt-2 text-xs text-negative">Live data must start before it ends.</p>}
        {saveSchedule.error && <p className="mt-2 text-xs text-negative">{errorText(saveSchedule.error)}</p>}
      </div>
    </Card>
  );
}
