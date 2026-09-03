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

// Looks up which curated Delta watchlist (Metals/DeFi/Meme/Smart Contract)
// a symbol belongs to, via the live membership map from useDeltaCategoryMap.
// null (not an "Other" catch-all) for anything not in one of those 4 lists
// -- a badge that read "Other" on every uncategorized row was just noise.
export function getDeltaCategory(symbol: string, categoryMap: Map<string, string> | undefined): string | null {
  return categoryMap?.get(symbol) ?? null;
}
