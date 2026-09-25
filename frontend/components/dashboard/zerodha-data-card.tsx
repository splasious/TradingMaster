"use client";

import { ChevronRight, Clock3, Database, Layers, LogIn, PauseCircle } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { fmtDay, fmtSavedUpTo, fmtTime, TIMEFRAME_NAMES } from "@/components/backfill-platform/overview/format";
import { behindLabel, FreshBadge, type FreshKind } from "@/components/backfill-platform/overview/fresh-status";
import { Card } from "@/components/ui/card";
import { useBfFreshness } from "@/lib/hooks";
import { cn } from "@/lib/utils";

function Fact({ icon: Icon, label, children, className }: { icon: typeof Clock3; label: string; children: ReactNode; className?: string }) {
  return (
    <div className="rounded-md border border-border px-2.5 py-2">
      <div className="flex items-center gap-1.5 text-[11.5px] text-text-muted">
        <Icon className="h-3.5 w-3.5" aria-hidden />
        {label}
      </div>
      <div className={cn("mt-0.5 font-financial text-[13px] font-medium text-text-primary", className)}>{children}</div>
    </div>
  );
}

/** Dashboard: how far Zerodha history is saved on the VPS, per timeframe. */
export function ZerodhaDataCard() {
  const { data } = useBfFreshness();
  const h = data?.headline;
  const tracked = data?.timeframes.filter((c) => c.status !== "none") ?? [];

  return (
    <Card>
      <div className="flex items-center justify-between px-4 pb-1 pt-3.5">
        <div className="flex items-center gap-2">
          <Database className="h-4 w-4 text-text-secondary" aria-hidden />
          <h3 className="text-sm font-semibold text-text-primary">Zerodha data on VPS</h3>
        </div>
        <Link href="/market-data" className="flex items-center gap-0.5 text-xs font-medium text-brand hover:underline">
          Data Backfill <ChevronRight className="h-3.5 w-3.5" />
        </Link>
      </div>
      <div className="px-4 pb-4">
        {!data ? (
          <p className="py-6 text-sm text-text-muted">Loading...</p>
        ) : !data.coverage_ready ? (
          <p className="py-6 text-sm text-text-muted">Counting what is saved -- the first time after an update takes a few minutes.</p>
        ) : !h?.saved_up_to ? (
          <p className="py-6 text-sm text-text-muted">No Zerodha data saved yet.</p>
        ) : (
          <>
            <div className="mt-1 text-xs text-text-muted">Complete, saved history up to</div>
            <div className="mt-0.5 whitespace-nowrap font-financial text-2xl font-semibold tracking-tight text-text-primary">
              {fmtSavedUpTo(h.saved_up_to)} IST
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              <FreshBadge kind={h.status as FreshKind} label={h.status === "ok" ? "Last closed session" : `${h.behind.length} timeframe${h.behind.length === 1 ? "" : "s"} behind`} />
              {data.queue.state === "running" ? (
                <FreshBadge kind="updating" label={`Top-up running${data.queue.percent != null ? ` · ${data.queue.percent}%` : ""}`} />
              ) : (
                data.live_today_until && <FreshBadge kind="ok" label={`Today live to ${fmtTime(data.live_today_until)}`} className="bg-active-soft text-active" />
              )}
            </div>

            <div className="mt-3">
              {tracked.map((c) => {
                const kind = (c.updating && c.status !== "ok" ? "updating" : c.status) as FreshKind;
                return (
                  <div key={c.timeframe} className="grid grid-cols-[72px_1fr_auto] items-center gap-2 border-b border-border py-1.5 text-[13px] last:border-b-0">
                    <span className="text-text-secondary">{TIMEFRAME_NAMES[c.timeframe]}</span>
                    <span className="font-financial text-text-secondary">{c.saved_up_to ? fmtSavedUpTo(c.saved_up_to, c.timeframe) : "--"}</span>
                    <FreshBadge kind={kind} label={kind === "updating" ? "Updating" : behindLabel(kind, c.sessions_behind, true)} />
                  </div>
                );
              })}
            </div>

            <div className="mt-3 grid grid-cols-2 gap-2">
              <Fact icon={Clock3} label="Next top-up">
                {data.next_run_at ? `${fmtDay(data.next_run_at)} ${fmtTime(data.next_run_at)}` : "Off"}
              </Fact>
              <Fact icon={LogIn} label="Zerodha login" className={data.zerodha_login.connected ? "text-positive" : "text-negative"}>
                {data.zerodha_login.connected ? "Connected" : "Log in needed"}
              </Fact>
              <Fact icon={Layers} label="Coverage">
                {data.symbols.zerodha.toLocaleString("en-IN")} NSE · {data.symbols.zerodha_nfo.toLocaleString("en-IN")} NFO
              </Fact>
              <Fact icon={PauseCircle} label="Delta Exchange" className="text-neutral">
                {data.delta_paused ? "Paused" : "Live"}
              </Fact>
            </div>
            <p className="mt-2.5 text-[11.5px] text-text-muted">Checked {fmtTime(data.as_of)} IST · refreshes every minute</p>
          </>
        )}
      </div>
    </Card>
  );
}
