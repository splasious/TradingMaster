"use client";

import { useState } from "react";

import { Badge, type Tone } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/data-state";
import { Select } from "@/components/ui/select";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { useAllLiveOrders } from "@/lib/hooks";

const STATUS_TONE: Record<string, Tone> = {
  created: "neutral",
  submitted: "warning",
  acknowledged: "warning",
  open: "active",
  partially_filled: "warning",
  filled: "positive",
  cancelled: "neutral",
  rejected: "critical",
  expired: "neutral",
};

export default function OrdersPage() {
  const { data: orders, isLoading, isError } = useAllLiveOrders();
  const [statusFilter, setStatusFilter] = useState("all");

  const statuses = Array.from(new Set((orders ?? []).map((o) => o.status))).sort();
  const filtered = statusFilter === "all" ? orders : orders?.filter((o) => o.status === statusFilter);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Orders</h1>
        <p className="text-sm text-text-muted">Every real order placed across all your live deployments, newest first.</p>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Order History</CardTitle>
          {!!statuses.length && (
            <Select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="w-40">
              <option value="all">All statuses</option>
              {statuses.map((s) => (
                <option key={s} value={s}>
                  {s.replace(/_/g, " ")}
                </option>
              ))}
            </Select>
          )}
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <LoadingState />
          ) : isError ? (
            <ErrorState description="Could not load orders." />
          ) : !filtered?.length ? (
            <EmptyState title="No live orders yet" description="Orders placed by live deployments will show up here." />
          ) : (
            <Table>
              <Thead>
                <tr>
                  <Th>Instrument</Th>
                  <Th>Strategy</Th>
                  <Th>Side</Th>
                  <Th>Quantity</Th>
                  <Th>Status</Th>
                  <Th>Broker Order ID</Th>
                  <Th>Placed</Th>
                  <Th>Confirmed</Th>
                </tr>
              </Thead>
              <Tbody>
                {filtered.map((o) => (
                  <tr key={o.id}>
                    <Td className="font-medium text-text-primary">{o.instrument_symbol}</Td>
                    <Td className="text-text-secondary">{o.strategy_name}</Td>
                    <Td className={o.side === "buy" ? "text-positive" : "text-negative"}>{o.side.toUpperCase()}</Td>
                    <Td className="font-financial">{o.quantity}</Td>
                    <Td>
                      <Badge tone={STATUS_TONE[o.status] ?? "neutral"}>{o.status.replace(/_/g, " ")}</Badge>
                      {o.reason && <p className="mt-1 max-w-xs text-xs text-text-muted">{o.reason}</p>}
                    </Td>
                    <Td className="font-financial text-text-muted">{o.broker_order_id ?? "--"}</Td>
                    <Td className="text-text-muted">{new Date(o.created_at).toLocaleString()}</Td>
                    <Td className="text-text-muted">{o.confirmed_at ? new Date(o.confirmed_at).toLocaleString() : "--"}</Td>
                  </tr>
                ))}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
