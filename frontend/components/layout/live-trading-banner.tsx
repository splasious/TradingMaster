import { AlertTriangle } from "lucide-react";

/** PRD section 49: live trading moves real money. This is a safety
 * requirement, not decoration -- critical-toned and impossible to miss,
 * distinct from the paper trading banner's neutral warning tone. */
export function LiveTradingBanner() {
  return (
    <div className="flex items-center justify-center gap-2 rounded-md border border-critical/40 bg-critical-soft px-4 py-2 text-sm font-semibold uppercase tracking-wide text-critical">
      <AlertTriangle className="h-4 w-4" />
      Live Trading -- real orders, real money, real broker
    </div>
  );
}
