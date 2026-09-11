"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/data-state";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { useLiveDeployments, useQuotes } from "@/lib/hooks";

export default function PositionsPage() {
  const { data: deployments, isLoading, isError } = useLiveDeployments();
  const open = (deployments ?? []).filter((d) => d.open_position);
  const { data: quotes } = useQuotes(open.map((d) => d.instrument_id));
  const lastCloseByInstrument = new Map((quotes ?? []).map((q) => [q.instrument_id, q.prev_close]));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Positions</h1>
        <p className="text-sm text-text-muted">
          Every open real position across your live deployments. Unrealized P&amp;L is estimated against the last stored
          daily close, not a live tick -- open the deployment in Live Trading for a current price.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Open Positions ({open.length})</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <LoadingState />
          ) : isError ? (
            <ErrorState description="Could not load positions." />
          ) : !open.length ? (
            <EmptyState title="No open live positions" description="Positions entered by live deployments will show up here." />
          ) : (
            <Table>
              <Thead>
                <tr>
                  <Th>Instrument</Th>
                  <Th>Strategy</Th>
                  <Th>Quantity</Th>
                  <Th>Avg Entry</Th>
                  <Th>Last Close</Th>
                  <Th>Unrealized P&amp;L</Th>
                  <Th>Opened</Th>
                </tr>
              </Thead>
              <Tbody>
                {open.map((d) => {
                  const position = d.open_position!;
                  const lastClose = lastCloseByInstrument.get(d.instrument_id);
                  const unrealized = lastClose != null ? (lastClose - position.avg_entry_price) * position.quantity : null;
                  const unrealizedPct = lastClose != null && position.avg_entry_price !== 0
                    ? ((lastClose - position.avg_entry_price) / position.avg_entry_price) * 100
                    : null;
                  return (
                    <tr key={d.id}>
                      <Td className="font-medium text-text-primary">{position.instrument_symbol}</Td>
                      <Td className="text-text-secondary">{d.strategy_name}</Td>
                      <Td className="font-financial">{position.quantity}</Td>
                      <Td className="font-financial">{position.avg_entry_price.toFixed(2)}</Td>
                      <Td className="font-financial text-text-muted">{lastClose != null ? lastClose.toFixed(2) : "--"}</Td>
                      <Td>
                        {unrealized != null ? (
                          <span className={`font-financial font-medium ${unrealized >= 0 ? "text-positive" : "text-negative"}`}>
                            {unrealized >= 0 ? "+" : ""}
                            {unrealized.toFixed(2)} {d.currency} ({unrealizedPct!.toFixed(1)}%)
                          </span>
                        ) : (
                          <span className="text-text-muted">--</span>
                        )}
                      </Td>
                      <Td className="text-text-muted">{new Date(position.opened_at).toLocaleString()}</Td>
                    </tr>
                  );
                })}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
