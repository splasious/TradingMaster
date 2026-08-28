import { type HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

export type Tone = "positive" | "negative" | "warning" | "critical" | "neutral" | "active" | "inactive";

const toneClasses: Record<Tone, string> = {
  positive: "bg-positive-soft text-positive",
  negative: "bg-negative-soft text-negative",
  warning: "bg-warning-soft text-warning",
  critical: "bg-critical-soft text-critical",
  neutral: "bg-neutral-soft text-neutral",
  active: "bg-active-soft text-active",
  inactive: "bg-inactive-soft text-inactive",
};

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

export function Badge({ className, tone = "neutral", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
        toneClasses[tone],
        className,
      )}
      {...props}
    />
  );
}
