"use client";

import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { ConnectionStatusBadge } from "@/components/ui/status-badge";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { useInstruments } from "@/lib/hooks";
import { getDeltaCategory, marketLabel } from "@/lib/market";
import { useMarketDataSocket } from "@/lib/ws";

export default function MarketsPage() {
  const [q, setQ] = useState("");
  const [exchange, setExchange] = useState("");
  const { data: instruments, isLoading, isError } = useInstruments(q, exchange || undefined);

  const instrumentIds = useMemo(() => (instruments ?? []).map((i) => i.id), [instruments]);
  const { status, prices, latencyMs } = useMarketDataSocket(instrumentIds);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Markets</h1>
          <p className="text-sm text-text-muted">NSE Markets (real history via nse-yahoo-data) and Delta Markets (real history, public API).</p>
        </div>
        <div className="flex items-center gap-3">
          {latencyMs !== null && <span className="font-financial text-xs text-text-muted">{latencyMs}ms</span>}
          <ConnectionStatusBadge status={status} />
        </div>
      </div>

      <div className="flex items-center gap-3">
        <Input placeholder="Search symbol or name..." value={q} onChange={(e) => setQ(e.target.value)} className="max-w-xs" />
        <Select value={exchange} onChange={(e) => setExchange(e.target.value)} className="w-40">
          <option value="">All Markets</option>
          <option value="NSE">NSE Markets</option>
          <option value="DELTA">Delta Markets</option>
        </Select>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Instruments</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <LoadingState />
          ) : isError ? (
            <ErrorState description="Could not load the instrument catalog." />
          ) : !instruments?.length ? (
            <EmptyState title="No instruments match" description="Try a different search term or market filter." />
          ) : (
            <Table>
              <Thead>
                <tr>
                  <Th>Market</Th>
                  <Th>Symbol</Th>
                  <Th>Name</Th>
                  <Th>Type</Th>
                  <Th className="text-right">Live Price</Th>
                </tr>
              </Thead>
              <Tbody>
                {instruments.map((instrument) => {
                  const tick = prices[instrument.id];
                  const category = instrument.exchange === "DELTA" ? getDeltaCategory(instrument.symbol) : null;
                  return (
                    <tr key={instrument.id}>
                      <Td>
                        <div className="flex items-center gap-1.5">
                          <Badge tone="neutral">{marketLabel(instrument.exchange)}</Badge>
                          {category && <Badge tone="active">{category}</Badge>}
                        </div>
                      </Td>
                      <Td className="font-medium">{instrument.symbol}</Td>
                      <Td className="text-text-secondary">{instrument.name}</Td>
                      <Td className="capitalize text-text-secondary">{instrument.instrument_type.replace("_", " ")}</Td>
                      <Td className="text-right">
                        {tick ? (
                          <span className="font-financial">
                            {tick.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                            <span className="ml-1.5 text-[10px] font-normal uppercase text-text-muted">simulated</span>
                          </span>
                        ) : (
                          <span className="text-text-muted">--</span>
                        )}
                      </Td>
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
