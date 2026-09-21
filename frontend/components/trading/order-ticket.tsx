"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { apiFetch, ApiError } from "@/lib/api";
import { useBrokerAccounts } from "@/lib/hooks";
import type { InstrumentOut } from "@/lib/types";

interface ManualOrderOut {
  id: string;
  instrument_symbol: string;
  broker_account_id: string;
  client_order_id: string;
  broker_order_id: string | null;
  side: string;
  quantity: number;
  product: string | null;
  status: string;
  reason: string | null;
  created_at: string;
  confirmed_at: string | null;
}

const FNO_TYPES = new Set(["option", "future"]);

/** Always-visible counterpart to fire-order-modal.tsx's dialog -- same
 * real order-submission logic (POST /api/v1/live-trading/orders/manual,
 * a genuine order on a connected live broker account), just rendered as
 * a persistent side panel for pages built around a Kite-style trade
 * ticket (Dashboard, Charts) instead of a modal triggered per-row.
 *
 * There is no equivalent manual "place a paper trade" endpoint anywhere
 * in this app -- paper positions only ever open through a deployed
 * strategy's own evaluate() cycle (see paper_trading/engine.py), never a
 * direct user click -- so this panel is deliberately labeled "Place Live
 * Order", not "Place Paper Trade": it would be actively misleading to
 * suggest a click here is paper-safe when it always fires a real order.
 *
 * Callers should render this keyed by `instrument.id` (see
 * fire-order-modal.tsx's identical convention, e.g.
 * `key={instrument?.id ?? "none"}`) so a fresh instrument fully remounts
 * the panel and every field resets on its own, without an effect
 * synchronizing state that a `key` already handles for free. */
export function OrderTicket({ instrument }: { instrument: InstrumentOut | null }) {
  const queryClient = useQueryClient();
  const { data: brokerAccounts } = useBrokerAccounts();
  const [brokerAccountId, setBrokerAccountId] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("");
  const [orderType, setOrderType] = useState<"market" | "limit">("market");
  const [limitPrice, setLimitPrice] = useState("");
  const isFno = instrument ? FNO_TYPES.has(instrument.instrument_type) : false;
  const [product, setProduct] = useState<"CNC" | "MIS" | "NRML">(isFno ? "MIS" : "CNC");
  const [confirmed, setConfirmed] = useState(false);

  const liveConnectedAccounts = brokerAccounts?.filter(
    (a) => a.connection_status === "connected" && a.environment === "live",
  );

  const fireMutation = useMutation({
    mutationFn: () =>
      apiFetch<ManualOrderOut>("/api/v1/live-trading/orders/manual", {
        method: "POST",
        body: JSON.stringify({
          instrument_id: instrument!.id,
          broker_account_id: brokerAccountId,
          side,
          quantity: Number(quantity),
          order_type: orderType,
          limit_price: orderType === "limit" ? Number(limitPrice) : null,
          product,
          confirmed: true,
        }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["live-orders"] });
      setQuantity("");
      setLimitPrice("");
      setConfirmed(false);
    },
  });

  const qtyValid =
    Number(quantity) > 0 && (!isFno || !instrument?.lot_size || Number(quantity) % instrument.lot_size === 0);
  const canSubmit =
    !!instrument &&
    !!brokerAccountId &&
    qtyValid &&
    confirmed &&
    (orderType === "market" || Number(limitPrice) > 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Place Live Order</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {!instrument ? (
          <p className="text-sm text-text-muted">Select an instrument to trade.</p>
        ) : (
          <>
            <div className="rounded-md bg-critical-soft px-3 py-2 text-xs text-critical">
              This places a real order on your real broker account using real money, right now -- with no strategy or
              risk-rule checklist behind it beyond the manual-order cap.
            </div>

            <div className="space-y-1.5">
              <p className="text-sm font-medium text-text-primary">{instrument.symbol}</p>
              <p className="text-xs text-text-muted">{instrument.name}</p>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <Button
                variant={side === "buy" ? "primary" : "secondary"}
                className={side === "buy" ? "bg-positive hover:opacity-90" : undefined}
                onClick={() => setSide("buy")}
              >
                Buy
              </Button>
              <Button
                variant={side === "sell" ? "primary" : "secondary"}
                className={side === "sell" ? "bg-negative text-white hover:opacity-90" : undefined}
                onClick={() => setSide("sell")}
              >
                Sell
              </Button>
            </div>

            <div className="space-y-1.5">
              <label className="text-xs font-medium text-text-secondary">Broker Account</label>
              <Select value={brokerAccountId} onChange={(e) => setBrokerAccountId(e.target.value)}>
                <option value="" disabled>
                  Select a connected live broker account
                </option>
                {liveConnectedAccounts?.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.broker.name} -- {a.account_label}
                  </option>
                ))}
              </Select>
              {!liveConnectedAccounts?.length && (
                <p className="text-xs text-text-muted">
                  No connected live broker accounts. Connect one under Settings &gt; Brokers first.
                </p>
              )}
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-text-secondary">
                  Quantity {isFno && instrument.lot_size ? <span className="text-text-muted">(lot {instrument.lot_size})</span> : null}
                </label>
                <Input type="number" min="0" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-text-secondary">Product</label>
                <Select value={product} onChange={(e) => setProduct(e.target.value as typeof product)}>
                  {isFno ? (
                    <>
                      <option value="MIS">MIS</option>
                      <option value="NRML">NRML</option>
                    </>
                  ) : (
                    <>
                      <option value="CNC">CNC</option>
                      <option value="MIS">MIS</option>
                    </>
                  )}
                </Select>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-text-secondary">Order Type</label>
                <Select value={orderType} onChange={(e) => setOrderType(e.target.value as "market" | "limit")}>
                  <option value="market">Market</option>
                  <option value="limit">Limit</option>
                </Select>
              </div>
              {orderType === "limit" && (
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-text-secondary">Limit Price</label>
                  <Input type="number" min="0" value={limitPrice} onChange={(e) => setLimitPrice(e.target.value)} />
                </div>
              )}
            </div>

            <label className="flex items-start gap-2 text-xs text-text-secondary">
              <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} className="mt-0.5" />
              I understand this places a real order with real money on my connected broker account.
            </label>

            {fireMutation.error && (
              <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
                {fireMutation.error instanceof ApiError ? fireMutation.error.message : "Order failed"}
              </div>
            )}

            {fireMutation.isSuccess && (
              <div className="rounded-md bg-positive-soft px-3 py-2 text-sm text-positive">
                Order submitted -- check Live Trading &gt; Orders for confirmed status.
              </div>
            )}

            <Button
              variant={side === "buy" ? "primary" : "destructive"}
              className={side === "buy" ? "w-full bg-positive hover:opacity-90" : "w-full"}
              disabled={!canSubmit || fireMutation.isPending}
              onClick={() => fireMutation.mutate()}
            >
              {fireMutation.isPending ? "Placing..." : `Place ${side === "buy" ? "Buy" : "Sell"} Order`}
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}
