import { FlaskConical } from "lucide-react";

/** PRD section 21: every paper-trading screen must clearly display that
 * it's paper trading, not real money -- this is a safety requirement, not
 * decoration, so it's a fixed, impossible-to-miss banner, not a badge that
 * blends into the header. */
export function PaperTradingBanner() {
  return (
    <div className="flex items-center justify-center gap-2 rounded-md border border-warning/30 bg-warning-soft px-4 py-2 text-sm font-semibold uppercase tracking-wide text-warning">
      <FlaskConical className="h-4 w-4" />
      Paper Trading -- simulated orders, no real money
    </div>
  );
}
