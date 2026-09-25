"use client";

import { CalendarClock, ChevronRight, RefreshCw, TriangleAlert } from "lucide-react";
import { useState, type ReactNode } from "react";

import { JobHistoryPanel } from "@/components/backfill-platform/job-history";
import { LiveSyncStatus } from "@/components/backfill-platform/live-sync-status";
import { SourceBlock } from "@/components/backfill-platform/source-block";
import { WatchlistsPanel } from "@/components/backfill-platform/watchlists-panel";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ErrorState, LoadingState } from "@/components/ui/data-state";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useBfOverview } from "@/lib/hooks";
import type { BfStorage } from "@/lib/types";
import { cn } from "@/lib/utils";

import { fmtBytes, fmtCount } from "./format";
import { FreshBadge } from "./fresh-status";
import { AttentionCard, JobQueueCard, ScheduleCard } from "./panels";
import { StocksTable } from "./stocks-table";
import { CoverageMatrix, KpiRow } from "./summary";
import { useBackfillActions } from "./use-backfill-actions";

function Section({ title, hint, right, children }: { title: string; hint?: string; right?: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-b border-border last:border-b-0">
      <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left">
        <span className="flex items-center gap-2">
          <ChevronRight className={cn("h-4 w-4 text-text-muted transition-transform", open && "rotate-90")} aria-hidden />
          <span className="text-[13px] font-medium text-text-primary">{title}</span>
          {hint && <span className="text-xs text-text-muted">{hint}</span>}
        </span>
        {right}
      </button>
      {open && <div className="space-y-4 px-4 pb-4">{children}</div>}
    </div>
  );
}

function StorageDetails({ storage }: { storage: BfStorage }) {
  const used = storage.disk_used_bytes / storage.disk_total_bytes;
  return (
    <div className="grid grid-cols-2 gap-3 text-[13px] md:grid-cols-4">
      <div><div className="text-xs text-text-muted">Database</div><div className="font-financial font-medium">{fmtBytes(storage.database_bytes)}</div></div>
      <div><div className="text-xs text-text-muted">Disk used</div><div className="font-financial font-medium">{Math.round(used * 100)}% of {fmtBytes(storage.disk_total_bytes)}</div></div>
      {Object.entries(storage.tables).map(([name, t]) => (
        <div key={name}>
          <div className="text-xs text-text-muted">{name === "bf_ohlcv_bars" ? "Backfill bars" : "Chart candles"}</div>
          <div className="font-financial font-medium">~{fmtCount(t.rows)} · {fmtBytes(t.bytes)}</div>
        </div>
      ))}
      <p className="col-span-full text-xs text-text-muted">
        Backups: the production database isn&apos;t backed up automatically yet -- a nightly off-server copy with a restore test is the next step.
      </p>
    </div>
  );
}

export function DataBackfillDashboard() {
  const { hasRole } = useAuth();
  const { data: o, isLoading, error } = useBfOverview();
  const { topUp } = useBackfillActions();
  const canOperate = hasRole("administrator", "trader", "analyst");
  const canEdit = hasRole("administrator");

  if (isLoading) return <LoadingState title="Loading data coverage..." />;
  if (error || !o) return <ErrorState description={error instanceof ApiError ? error.message : "Couldn't load the backfill overview."} />;

  const storageSummary = o.storage
    ? `database ${fmtBytes(o.storage.database_bytes)} · disk ${Math.round((100 * o.storage.disk_used_bytes) / o.storage.disk_total_bytes)}% used`
    : undefined;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-text-primary">Data Backfill</h2>
          <p className="text-sm text-text-muted">Zerodha price history stored on the VPS database -- what is saved, how fresh it is, and what runs next.</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={() => document.getElementById("automatic-schedule")?.scrollIntoView({ behavior: "smooth" })}>
            <CalendarClock className="h-3.5 w-3.5" /> Schedule
          </Button>
          {canOperate && (
            <Button
              size="sm"
              disabled={topUp.isPending || !o.zerodha_login.connected}
              title={o.zerodha_login.connected ? "Fetch everything new since the last saved candle" : "Log in to Zerodha first"}
              onClick={() => topUp.mutate(undefined)}
            >
              <RefreshCw className={cn("h-3.5 w-3.5", topUp.isPending && "animate-spin")} /> Top up now
            </Button>
          )}
        </div>
      </div>
      {topUp.error && <p className="text-sm text-negative">{topUp.error instanceof ApiError ? topUp.error.message : "Top-up failed to start"}</p>}

      <KpiRow o={o} />
      <CoverageMatrix segments={o.segments} />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_1fr_380px]">
        <JobQueueCard queue={o.queue} canOperate={canOperate} />
        <AttentionCard items={o.attention} canOperate={canOperate} />
        <ScheduleCard schedule={o.schedule} canEdit={canEdit} />
      </div>

      <StocksTable />

      <Card>
        <Section title="Manual backfill" hint="pick symbols, timeframes and a date range" right={<span className="text-xs text-text-muted">Advanced</span>}>
          <SourceBlock source="zerodha" />
          <SourceBlock source="zerodha_nfo" />
          <SourceBlock source="delta" />
          <LiveSyncStatus />
        </Section>
        <Section title="Watchlists">
          <WatchlistsPanel />
        </Section>
        <Section title="Job history" hint="every job, with the reason for any failure">
          <JobHistoryPanel />
        </Section>
        <Section
          title="Storage"
          hint={storageSummary}
          right={o.storage && !o.storage.backups_verified ? <FreshBadge kind="warn" label="Backups not verified" /> : undefined}
        >
          {o.storage ? <StorageDetails storage={o.storage} /> : <p className="text-[13px] text-text-muted">Storage figures are shown for the PostgreSQL database.</p>}
        </Section>
      </Card>
      {!o.coverage_ready && (
        <p className="flex items-center gap-2 text-xs text-warning">
          <TriangleAlert className="h-3.5 w-3.5" /> Counting what is saved for the first time -- figures fill in over the next few minutes.
        </p>
      )}
    </div>
  );
}
