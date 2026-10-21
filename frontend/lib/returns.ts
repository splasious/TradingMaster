/** Return measures for a strategy card's trade record (21 Oct): ROI on the
 * pool's starting capital, and its annualised rate as CAGR and XIRR.
 *
 * XIRR solves for the yearly rate that discounts dated cash flows to zero.
 * A paper pool's only flows are its capital going in when the card started
 * and capital + net P&L today (a capital change resets the pool rather than
 * adding money), so its XIRR equals its CAGR; it differs once deposits or
 * withdrawals exist. Over a short run both annualise a few weeks into a
 * year, so a card shows how many days they're from. */

const DAY_MS = 86_400_000;
export const MEANINGFUL_DAYS = 90;

export interface CashFlow {
  amount: number; // money in negative (invested), money out positive
  at: Date;
}

export function roiPct(netPnl: number, capital: number): number | null {
  return capital > 0 ? (netPnl / capital) * 100 : null;
}

export function cagrPct(start: number, end: number, days: number): number | null {
  if (start <= 0 || end <= 0 || days < 1) return null;
  return (Math.pow(end / start, 365 / days) - 1) * 100;
}

/** The annual rate (in %) at which these flows' present value is zero, or
 * null when there's no such rate (all one sign, or under a day). */
export function xirrPct(flows: CashFlow[]): number | null {
  if (flows.length < 2 || !flows.some((f) => f.amount < 0) || !flows.some((f) => f.amount > 0)) return null;
  const t0 = Math.min(...flows.map((f) => f.at.getTime()));
  const years = flows.map((f) => (f.at.getTime() - t0) / DAY_MS / 365);
  if (Math.max(...years) < 1 / 365) return null;
  const value = (rate: number) => flows.reduce((sum, f, i) => sum + f.amount / Math.pow(1 + rate, years[i]), 0);
  // Present value falls as the rate rises (money in comes first): bisect.
  let lo = -0.9999;
  let hi = 1;
  while (value(hi) > 0 && hi < 1e9) hi *= 2;
  if (value(lo) < 0 || value(hi) > 0) return null;
  for (let i = 0; i < 200; i++) {
    const mid = (lo + hi) / 2;
    if (value(mid) > 0) lo = mid;
    else hi = mid;
  }
  return ((lo + hi) / 2) * 100;
}

export function daysBetween(from: Date, to: Date): number {
  return (to.getTime() - from.getTime()) / DAY_MS;
}

/** "+24.59%", "+1,450%" -- whole numbers once it's in the hundreds. */
export function formatPct(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (Math.abs(value) < 0.005) value = 0; // rounding noise, not a loss
  const digits = Math.abs(value) >= 100 ? 0 : 2;
  const text = Math.abs(value).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${text}%`;
}
