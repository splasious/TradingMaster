import type { CompletenessSegmentOut } from "@/lib/types";

/** Real per-day segments from the backend (PRD section 6's calendar-heatmap
 * requirement) rendered as a proportional horizontal bar -- green = filled,
 * red = gap, each block sized to its actual day-span and carrying the real
 * date range + status in a native tooltip on hover. */
export function CompletenessHeatmap({ segments, rangeStart, rangeEnd }: { segments: CompletenessSegmentOut[]; rangeStart: string; rangeEnd: string }) {
  if (!segments.length) return <p className="text-xs text-text-muted">No trading days in this range.</p>;

  const totalDays = (new Date(rangeEnd).getTime() - new Date(rangeStart).getTime()) / 86_400_000 + 1;

  return (
    <div className="space-y-1.5">
      <div className="flex h-4 w-full overflow-hidden rounded-md border border-border">
        {segments.map((seg, i) => {
          const days = (new Date(seg.end).getTime() - new Date(seg.start).getTime()) / 86_400_000 + 1;
          const widthPct = Math.max(0.5, (days / totalDays) * 100);
          return (
            <div
              key={i}
              title={`${seg.status === "filled" ? "Filled" : "Gap"}: ${seg.start} to ${seg.end} (${days} day${days > 1 ? "s" : ""})`}
              className={seg.status === "filled" ? "bg-positive" : "bg-negative"}
              style={{ width: `${widthPct}%` }}
            />
          );
        })}
      </div>
      <div className="flex items-center justify-between text-[11px] text-text-muted">
        <span>{rangeStart}</span>
        <span className="flex items-center gap-3">
          <span className="flex items-center gap-1">
            <span className="h-2 w-2 rounded-sm bg-positive" /> Filled
          </span>
          <span className="flex items-center gap-1">
            <span className="h-2 w-2 rounded-sm bg-negative" /> Gap
          </span>
        </span>
        <span>{rangeEnd}</span>
      </div>
    </div>
  );
}
