"use client";

import { CheckCircle2, Loader2, TriangleAlert, XCircle } from "lucide-react";
import Link from "next/link";

import { fmtSavedUpTo, TIMEFRAME_SHORT } from "@/components/backfill-platform/overview/format";
import { useBfFreshness } from "@/lib/hooks";
import { cn } from "@/lib/utils";

const TONES = {
  ok: { className: "bg-positive-soft text-positive", Icon: CheckCircle2 },
  warn: { className: "bg-warning-soft text-warning", Icon: TriangleAlert },
  bad: { className: "bg-negative-soft text-negative", Icon: XCircle },
} as const;

/** "Zerodha data saved up to Thu 24 Sep 15:30" -- how far the VPS database
 * has Zerodha history, on every page; a timeframe that is behind is named. */
export function DataFreshnessPill() {
  const { data } = useBfFreshness();
  const h = data?.headline;
  const savedUpTo = h?.saved_up_to;
  if (!data?.coverage_ready || !h || !savedUpTo) return null;

  const updating = data.queue.state === "running";
  const tone = TONES[(h.status in TONES ? h.status : "ok") as keyof typeof TONES];
  const Icon = updating ? Loader2 : tone.Icon;
  const lagging = h.behind.map((b) => TIMEFRAME_SHORT[b.timeframe]).join(", ");

  return (
    <Link
      href="/market-data"
      title="Open Data Backfill"
      className={cn(
        "flex min-w-0 items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-1 text-xs font-medium hover:opacity-90 sm:gap-2 sm:px-2.5",
        updating ? "bg-active-soft text-active" : tone.className,
      )}
    >
      <Icon className={cn("h-3.5 w-3.5 shrink-0", updating && "animate-spin")} aria-hidden />
      <span className="hidden md:inline">Zerodha data saved up to</span>
      <b className="truncate font-financial font-semibold text-text-primary">{fmtSavedUpTo(savedUpTo)}</b>
      {updating && data.queue.percent != null && <span className="hidden font-financial sm:inline">· updating {data.queue.percent}%</span>}
      {!updating && lagging && (
        <span className="hidden items-center gap-1 border-l border-current/25 pl-2 text-warning sm:flex">
          <TriangleAlert className="h-3 w-3" aria-hidden />
          {lagging} behind
        </span>
      )}
    </Link>
  );
}
