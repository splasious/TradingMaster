"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/data-state";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { usePaperDeployments, useNativeDeployments } from "@/lib/hooks";

interface Row {
  key: string;
  symbol: string;
  strategy: string;
  quantity: number | null;
  avgEntry: number | null;
  current: number | null;
  unrealizedPnl: number | null;
  currency: string;
  openedAt: string;
}

/** Every open paper position across both regular (single-instrument) and
 * Advanced Python (native, possibly multi-leg) deployments, in one table
 * -- paper trading is this app's primary/default mode (see
 * app/(app)/paper-trading's own "Today's Gain" card), so this is what a
 * Kite-style Dashboard's positions panel should show, unlike the
 * existing Positions page which is live-trading-only. A native
 * deployment's multi-leg spread renders as one row (its own
 * server-computed aggregate unrealized_pnl, see NativePositionOut); a
 * multi-holding deployment (independent long-only legs, no single
 * position to aggregate) renders one row per holding, matching how
 * paper-trading/page.tsx's PortfolioCard sums the same shape. */
export function PaperPositionsTable() {
  const { data: deployments, isLoading: loadingRegular } = usePaperDeployments();
  const { data: nativeDeployments, isLoading: loadingNative } = useNativeDeployments();
  const isLoading = loadingRegular || loadingNative;

  const rows: Row[] = [];

  for (const d of deployments ?? []) {
    if (!d.open_position) continue;
    const p = d.open_position;
    rows.push({
      key: d.id,
      symbol: p.instrument_symbol,
      strategy: d.strategy_name,
      quantity: p.quantity,
      avgEntry: p.avg_entry_price,
      current: p.current_price,
      unrealizedPnl: p.unrealized_pnl,
      currency: d.currency,
      openedAt: p.opened_at,
    });
  }

  for (const d of nativeDeployments ?? []) {
    if (d.position) {
      const legSummary = d.position.legs.map((l) => l.instrument_symbol).join(" / ");
      rows.push({
        key: d.id,
        symbol: `${legSummary} (${d.position.bias ?? "spread"})`,
        strategy: d.strategy_name,
        // A multi-leg spread has no single per-unit entry/current price --
        // trade_value/live_value are net credit-or-debit totals for the
        // whole spread, not comparable to a single-instrument row's Avg
        // Entry/LTP columns, so those are left blank here rather than
        // showing a number in the wrong units. unrealized_pnl (below) is
        // the one figure that's still directly comparable.
        quantity: null,
        avgEntry: null,
        current: null,
        unrealizedPnl: d.position.unrealized_pnl,
        currency: d.currency,
        openedAt: d.position.opened_at,
      });
    }
    for (const leg of d.holdings ?? []) {
      const unrealizedPnl = leg.current_price != null ? (leg.current_price - leg.entry_price) * leg.quantity : null;
      rows.push({
        key: `${d.id}-${leg.instrument_symbol}`,
        symbol: leg.instrument_symbol,
        strategy: d.strategy_name,
        quantity: leg.quantity,
        avgEntry: leg.entry_price,
        current: leg.current_price,
        unrealizedPnl,
        currency: d.currency,
        openedAt: d.created_at,
      });
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Positions ({rows.length})</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        {isLoading ? (
          <LoadingState />
        ) : !rows.length ? (
          <EmptyState title="No open positions" description="Positions opened by active paper deployments will show up here." />
        ) : (
          <Table>
            <Thead>
              <tr>
                <Th>Symbol</Th>
                <Th>Strategy</Th>
                <Th>Qty</Th>
                <Th>Avg Entry</Th>
                <Th>LTP</Th>
                <Th>Unrealized P&amp;L</Th>
              </tr>
            </Thead>
            <Tbody>
              {rows.map((row) => (
                <tr key={row.key}>
                  <Td className="font-medium text-text-primary">{row.symbol}</Td>
                  <Td className="text-text-secondary">{row.strategy}</Td>
                  <Td className="font-financial">{row.quantity ?? "--"}</Td>
                  <Td className="font-financial">{row.avgEntry != null ? row.avgEntry.toFixed(2) : "--"}</Td>
                  <Td className="font-financial text-text-muted">{row.current != null ? row.current.toFixed(2) : "--"}</Td>
                  <Td>
                    {row.unrealizedPnl != null ? (
                      <span className={`font-financial font-medium ${row.unrealizedPnl >= 0 ? "text-positive" : "text-negative"}`}>
                        {row.unrealizedPnl >= 0 ? "+" : ""}
                        {row.unrealizedPnl.toFixed(2)} {row.currency}
                      </span>
                    ) : (
                      <span className="text-text-muted">--</span>
                    )}
                  </Td>
                </tr>
              ))}
            </Tbody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
