import type { ClosePriceOut } from "@/lib/types";

/** While NSE is shut the server prices open legs at the session's close
 * (backend market_data/closing_price.py) and says so on each leg; these
 * turn that into the column headings and the provisional marker. */

type Priced = { close?: ClosePriceOut | null };

export const PROVISIONAL_NOTE = "15:30 last price; the day's closing price replaces it after the evening download (about 18:00).";

/** "5 Oct" for a session date such as "2026-10-05". */
export function sessionDay(session: string): string {
  const [y, m, d] = session.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-IN", { day: "numeric", month: "short", timeZone: "UTC" });
}

function sessions(legs: Priced[]): string[] {
  return [...new Set(legs.map((l) => l.close?.session).filter((s): s is string => !!s))].sort();
}

/** "LTP" while the market is open; "Close · 5 Oct" while it's shut. */
export function priceHeading(legs: Priced[]): string {
  const days = sessions(legs);
  if (days.length === 0) return "LTP";
  return days.length === 1 ? `Close · ${sessionDay(days[0])}` : "Close";
}

export function valueHeading(legs: Priced[]): string {
  return sessions(legs).length ? "Value at Close" : "Live Value";
}

/** After the price: its day when the rows close on different days, and
 * "*" while it's still the 15:30 last price. */
export function CloseMark({ close, legs }: { close?: ClosePriceOut | null; legs: Priced[] }) {
  if (!close) return null;
  return (
    <>
      {sessions(legs).length > 1 && <span className="ml-1 text-text-muted">({sessionDay(close.session)})</span>}
      {close.provisional && (
        <span className="ml-0.5 text-text-muted" title={PROVISIONAL_NOTE}>
          *
        </span>
      )}
    </>
  );
}

export function CloseFootnote({ legs }: { legs: Priced[] }) {
  if (!legs.some((l) => l.close?.provisional)) return null;
  return <p className="border-t border-border px-3 py-1.5 text-[11px] text-text-muted">* {PROVISIONAL_NOTE}</p>;
}
