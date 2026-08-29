/** Market/instrument-category labeling, kept in one place so "Delta
 * Markets" (never "Crypto") and BTC/ETH/SOL categorization stay
 * consistent everywhere they're rendered. Purely a UI label layer --
 * the underlying data model still just has `exchange`/`symbol` strings. */

export type MarketExchange = "NSE" | "DELTA";

export function marketLabel(exchange: string): string {
  if (exchange === "NSE") return "NSE Markets";
  if (exchange === "DELTA") return "Delta Markets";
  return exchange;
}

export function brokerForExchange(exchange: string): string {
  if (exchange === "NSE") return "Zerodha Kite";
  if (exchange === "DELTA") return "Delta Exchange";
  return "--";
}

// Derived from the symbol prefix (e.g. "BTCUSD" -> "BTC") since the
// instrument model has no dedicated category/base-asset field yet --
// deliberately not inventing new backend data for a UI-only pass.
const DELTA_CATEGORY_PREFIXES = ["BTC", "ETH", "SOL"] as const;
export type DeltaCategory = (typeof DELTA_CATEGORY_PREFIXES)[number] | "Other";

export function getDeltaCategory(symbol: string): DeltaCategory {
  const match = DELTA_CATEGORY_PREFIXES.find((prefix) => symbol.toUpperCase().startsWith(prefix));
  return match ?? "Other";
}
