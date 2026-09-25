"use client";

import { Activity, Database, LogIn, RefreshCw } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { Card } from "@/components/ui/card";
import type { BfCoverageCell, BfOverview, BfSegment } from "@/lib/types";
import { cn } from "@/lib/utils";

import { fmtCount, fmtDay, fmtEta, fmtSavedUpTo, fmtTime, TIMEFRAME_NAMES, TIMEFRAME_SHORT } from "./format";
import { behindLabel, FreshBadge, type FreshKind } from "./fresh-status";

function Tile({ icon: Icon, label, children, className }: { icon: typeof Database; label: string; children: ReactNode; className?: string }) {
  return (
    <Card className={cn("px-4 py-3.5", className)}>
      <div className="flex items-center gap-1.5 text-xs text-text-muted">
        <Icon className="h-3.5 w-3.5" aria-hidden />
        {label}
      </div>
      {children}
    </Card>
  );
}

export function Meter({ percent }: { percent: number }) {
  return (
    <div className="h-2 overflow-hidden rounded-full bg-active-soft" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
      <div className="h-full rounded-full bg-active transition-[width]" style={{ width: `${Math.max(2, Math.min(100, percent))}%` }} />
    </div>
  );
}

function headlineMeta(o: BfOverview): string {
  const h = o.headline;
  const lagging = h.behind.map((b) => `${TIMEFRAME_SHORT[b.timeframe]} is ${behindLabel(b.sessions_behind > 1 ? "bad" : "warn", b.sessions_behind)}`);
  const count = `${h.timeframes_current} of ${h.timeframes} timeframe${h.timeframes === 1 ? "" : "s"} current`;
  return lagging.length ? `${count} · ${lagging.join(", ")}` : count;
}

export function KpiRow({ o }: { o: BfOverview }) {
  const h = o.headline;
  const q = o.queue;
  const nextRun = o.schedule.next_run_at;
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-[1.35fr_1fr_1fr_1fr]">
      <Tile icon={Database} label="Zerodha data saved on VPS up to">
        {!o.coverage_ready ? (
          <p className="mt-2 text-sm text-text-secondary">Counting what is saved -- the first time after the update takes a few minutes.</p>
        ) : h.saved_up_to ? (
          <>
            <div className="mt-1.5 whitespace-nowrap font-financial text-2xl font-semibold tracking-tight text-text-primary">
              {fmtSavedUpTo(h.saved_up_to)} IST
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1">
              <FreshBadge
                kind={h.status as FreshKind}
                label={h.status === "ok" ? "Last closed session" : `${h.behind.length} timeframe${h.behind.length === 1 ? "" : "s"} behind`}
              />
              <span className="text-xs text-text-secondary">{headlineMeta(o)}</span>
            </div>
          </>
        ) : (
          <p className="mt-2 text-sm text-text-secondary">No Zerodha data saved yet -- start a backfill below.</p>
        )}
      </Tile>

      <Tile icon={RefreshCw} label="Top-up">
        {q.state === "running" && q.run ? (
          <>
            <div className="mt-1.5 font-financial text-xl font-semibold text-text-primary">Running · {q.run.percent}%</div>
            <div className="mt-2.5"><Meter percent={q.run.percent} /></div>
            <p className="mt-1.5 font-financial text-xs text-text-secondary">
              {(q.run.done + q.run.failed).toLocaleString("en-IN")} of {q.run.total.toLocaleString("en-IN")} jobs
              {fmtEta(q.run.eta_seconds) ? ` · ${fmtEta(q.run.eta_seconds)}` : ""}
            </p>
          </>
        ) : q.state === "waiting_login" ? (
          <>
            <div className="mt-1.5 text-xl font-semibold text-warning">Waiting for login</div>
            <p className="mt-1.5 text-xs text-text-secondary">Starts as soon as Zerodha is logged in.</p>
          </>
        ) : q.state === "paused" ? (
          <>
            <div className="mt-1.5 text-xl font-semibold text-neutral">Paused</div>
            <p className="mt-1.5 text-xs text-text-secondary">{q.queued_total.toLocaleString("en-IN")} jobs waiting -- resume from the queue.</p>
          </>
        ) : (
          <>
            <div className="mt-1.5 font-financial text-xl font-semibold text-text-primary">
              {nextRun ? `Next ${fmtDay(nextRun)} ${fmtTime(nextRun)}` : "Automatic top-up off"}
            </div>
            <p className="mt-1.5 text-xs text-text-secondary">
              {q.last_run?.completed_at
                ? `Last: ${fmtDay(q.last_run.completed_at)} ${fmtTime(q.last_run.completed_at)} · ${q.last_run.message ?? q.last_run.status}`
                : "No top-up has run yet."}
            </p>
          </>
        )}
      </Tile>

      <Tile icon={Activity} label="Today (live chart sync)">
        <div className="mt-1.5 font-financial text-xl font-semibold text-text-primary">
          {o.live_today_until ? `Up to ${fmtTime(o.live_today_until)} IST` : "No live data today"}
        </div>
        <p className="mt-1.5 text-xs text-text-secondary">
          {o.live_today_until
            ? "5m & 15m for charts and strategies -- the daily top-up makes the day final."
            : "Today's candles arrive during market hours (09:15-15:30 IST)."}
        </p>
      </Tile>

      <Tile icon={LogIn} label="Zerodha login">
        <div className={cn("mt-1.5 text-xl font-semibold", o.zerodha_login.connected ? "text-text-primary" : "text-negative")}>
          {o.zerodha_login.connected ? "Connected" : o.zerodha_login.status === "expired" ? "Logged out" : "Not set up"}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {o.zerodha_login.connected ? (
            <>
              <FreshBadge kind="ok" label="Valid" />
              <span className="text-xs text-text-secondary">Kite logins end ~06:00 IST daily</span>
            </>
          ) : (
            <Link href="/settings" className="text-xs font-medium text-brand hover:underline">
              Log in with Zerodha in Settings
            </Link>
          )}
        </div>
      </Tile>
    </div>
  );
}

function cellKind(cell: BfCoverageCell): FreshKind {
  if (cell.updating && cell.status !== "ok") return "updating";
  return cell.status as FreshKind;
}

function CoverageCell({ cell, unit }: { cell: BfCoverageCell; unit: string }) {
  if (cell.status === "none" || !cell.saved_up_to) {
    return <span className="text-xs text-text-muted">Not tracked</span>;
  }
  const kind = cellKind(cell);
  const label = kind === "updating" ? "Updating" : behindLabel(kind, cell.sessions_behind, true);
  const lines: string[] = [];
  if (cell.symbols) {
    lines.push(cell.behind ? `${cell.current?.toLocaleString("en-IN")} of ${cell.symbols.toLocaleString("en-IN")} ${unit} current` : `${cell.symbols.toLocaleString("en-IN")} ${unit}`);
  }
  if (cell.expired) lines.push(`${cell.expired.toLocaleString("en-IN")} expired, kept`);
  if (cell.bars) lines.push(`${fmtCount(cell.bars)} bars`);
  return (
    <div>
      <FreshBadge kind={kind} label={label} />
      <div className="mt-1.5 whitespace-nowrap font-financial text-[13px] text-text-primary">{fmtSavedUpTo(cell.saved_up_to, cell.timeframe)}</div>
      <div className="mt-0.5 text-[11.5px] leading-snug text-text-muted">{lines.join(" · ")}</div>
    </div>
  );
}

export function CoverageMatrix({ segments }: { segments: BfSegment[] }) {
  const kite = segments.filter((s) => s.source !== "delta");
  const delta = segments.find((s) => s.source === "delta");
  const timeframes = kite[0]?.cells.map((c) => c.timeframe) ?? [];
  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 pb-2.5 pt-3.5">
        <h3 className="text-sm font-semibold text-text-primary">Coverage by timeframe</h3>
        <div className="flex flex-wrap gap-2">
          <FreshBadge kind="ok" label="Up to date" />
          <FreshBadge kind="warn" label="1 session behind" />
          <FreshBadge kind="bad" label="2+ sessions behind" />
          <FreshBadge kind="paused" label="Paused" />
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[860px] border-collapse text-left">
          <thead>
            <tr className="border-y border-border">
              <th className="w-48 px-4 py-2 text-[11px] font-medium uppercase tracking-wide text-text-muted">Segment</th>
              {timeframes.map((tf) => (
                <th key={tf} className="border-l border-border px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-text-muted">
                  {TIMEFRAME_NAMES[tf]}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {kite.map((seg) => (
              <tr key={seg.source} className="border-b border-border align-top">
                <td className="px-4 py-3">
                  <div className="text-sm font-semibold text-text-primary">{seg.label}</div>
                  <div className="mt-0.5 text-xs text-text-muted">
                    {seg.symbols.toLocaleString("en-IN")} {seg.unit}
                    {seg.enabled === false ? " · auto top-up off" : ""}
                  </div>
                </td>
                {seg.cells.map((cell) => (
                  <td key={cell.timeframe} className="border-l border-border px-3 py-3">
                    <CoverageCell cell={cell} unit={seg.unit} />
                  </td>
                ))}
              </tr>
            ))}
            {delta && (
              <tr className="bg-[repeating-linear-gradient(135deg,var(--inactive-soft)_0_6px,transparent_6px_12px)]">
                <td className="px-4 py-3">
                  <div className="text-sm font-semibold text-text-primary">{delta.label}</div>
                  <div className="mt-0.5 text-xs text-text-muted">
                    {delta.symbols.toLocaleString("en-IN")} {delta.unit}
                    {delta.paused ? " · frozen" : ""}
                  </div>
                </td>
                <td colSpan={timeframes.length} className="border-l border-border px-3 py-3">
                  <div className="flex flex-wrap items-center gap-2 text-[13px] text-text-secondary">
                    {delta.paused ? (
                      <>
                        <FreshBadge kind="paused" label="Paused" />
                        No new data is fetched while paused. Everything already saved is kept
                        {delta.last_saved_at ? ` (last saved ${fmtDay(delta.last_saved_at)} ${fmtTime(delta.last_saved_at)})` : ""}. Resume it
                        from Automatic schedule.
                      </>
                    ) : (
                      <>
                        <FreshBadge kind="ok" label="Live" />
                        1-minute candles sync every minute; other timeframes on demand.
                      </>
                    )}
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
