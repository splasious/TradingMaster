import { CheckCircle2, CircleDashed, Clock3, Loader2, Minus, PauseCircle, TriangleAlert, XCircle } from "lucide-react";

import { Badge, type Tone } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export type FreshKind = "ok" | "warn" | "bad" | "none" | "paused" | "updating" | "expired" | "waiting";

const KINDS: Record<FreshKind, { tone: Tone; Icon: typeof CheckCircle2; text: string }> = {
  ok: { tone: "positive", Icon: CheckCircle2, text: "text-positive" },
  warn: { tone: "warning", Icon: TriangleAlert, text: "text-warning" },
  bad: { tone: "negative", Icon: XCircle, text: "text-negative" },
  none: { tone: "inactive", Icon: Minus, text: "text-text-muted" },
  paused: { tone: "neutral", Icon: PauseCircle, text: "text-neutral" },
  updating: { tone: "active", Icon: Loader2, text: "text-active" },
  expired: { tone: "inactive", Icon: CircleDashed, text: "text-text-muted" },
  waiting: { tone: "warning", Icon: Clock3, text: "text-warning" },
};

/** "3 sessions behind" / "Up to date" -- a freshness label for a status. */
export function behindLabel(kind: FreshKind, sessions?: number, short = false): string {
  if (kind === "ok") return "Up to date";
  if (kind === "warn" || kind === "bad") {
    const n = sessions ?? 1;
    return short ? `${n} behind` : `${n} session${n === 1 ? "" : "s"} behind`;
  }
  if (kind === "paused") return "Paused";
  if (kind === "updating") return "Updating";
  if (kind === "expired") return "Expired";
  if (kind === "waiting") return "Waiting";
  return "Not tracked";
}

/** Status is always icon + label + colour together -- never colour alone. */
export function FreshBadge({ kind, label, className }: { kind: FreshKind; label: string; className?: string }) {
  const { tone, Icon } = KINDS[kind];
  return (
    <Badge tone={tone} className={cn("px-2 py-0.5", className)}>
      <Icon className={cn("h-3.5 w-3.5", kind === "updating" && "animate-spin")} aria-hidden />
      {label}
    </Badge>
  );
}

/** A bare icon + text (no pill) for dense tables. */
export function FreshInline({ kind, label }: { kind: FreshKind; label: string }) {
  const { Icon, text } = KINDS[kind];
  return (
    <span className={cn("inline-flex items-center gap-1.5 font-financial text-xs", text)}>
      <Icon className={cn("h-3.5 w-3.5 shrink-0", kind === "updating" && "animate-spin")} aria-hidden />
      {label}
    </span>
  );
}
