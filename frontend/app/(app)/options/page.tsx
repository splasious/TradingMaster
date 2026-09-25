"use client";

import { useRouter, useSearchParams } from "next/navigation";

import { ChainOiView } from "@/components/options/chain-oi-view";
import { PcrAnalysisView } from "@/components/options/pcr-analysis-view";
import { cn } from "@/lib/utils";

const TABS = [
  { id: "chain", label: "Chain & OI" },
  { id: "pcr", label: "PCR Analysis" },
] as const;

/** Options: two tabs, kept in the address (?tab=pcr) so a link or a
 * reload opens the same one. */
export default function OptionsPage() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const tab = searchParams.get("tab") === "pcr" ? "pcr" : "chain";

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Options Dashboard</h1>
        <p className="text-sm text-text-muted">
          {tab === "pcr"
            ? "NIFTY PCR from open interest and its change, ATM ±40 strikes, recorded every 15 minutes (09:00–15:30 IST)."
            : "NFO option chain, PCR, and open interest -- live via Kite WebSocket where connected."}
        </p>
      </div>
      <div className="flex gap-1 border-b border-border" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => router.replace(t.id === "pcr" ? "/options?tab=pcr" : "/options", { scroll: false })}
            className={cn(
              "-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors",
              tab === t.id ? "border-brand text-text-primary" : "border-transparent text-text-muted hover:text-text-secondary",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "pcr" ? <PcrAnalysisView /> : <ChainOiView />}
    </div>
  );
}
