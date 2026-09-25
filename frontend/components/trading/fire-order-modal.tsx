"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
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

/** Keyed by `instrument.id` at the call site (see scanner/page.tsx) so a
 * fresh instrument fully remounts this component -- every field, including
 * the instrument_type-dependent `product` default below, resets on its own
 * without a manual reset() or a setState-in-effect. */
export function FireOrderModal({ instrument, onClose }: { instrument: InstrumentOut | null; onClose: () => void }) {
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
    (a) => a.connection_status === "connected" && a.environment === "live" && a.broker.supports_trading !== false,
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
    <Modal open={!!instrument} onClose={onClose} title={`Fire Order -- ${instrument?.symbol ?? ""}`}>
      <div className="space-y-4">
        <div className="rounded-md bg-critical-soft px-3 py-2 text-xs text-critical">
          This places a real order on your real broker account using real money, right now -- with no strategy or
          risk-rule checklist behind it beyond the manual-order cap.
        </div>

        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Broker Account</label>
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

        <div className="flex gap-3">
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">Side</label>
            <Select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")} className="w-28">
              <option value="buy">Buy</option>
              <option value="sell">Sell</option>
            </Select>
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">
              Quantity {isFno && instrument?.lot_size ? <span className="text-text-muted">(lot size {instrument.lot_size})</span> : null}
            </label>
            <Input type="number" min="0" value={quantity} onChange={(e) => setQuantity(e.target.value)} className="w-28" />
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">Product</label>
            <Select value={product} onChange={(e) => setProduct(e.target.value as typeof product)} className="w-24">
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

        <div className="flex gap-3">
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-text-secondary">Order Type</label>
            <Select value={orderType} onChange={(e) => setOrderType(e.target.value as "market" | "limit")} className="w-28">
              <option value="market">Market</option>
              <option value="limit">Limit</option>
            </Select>
          </div>
          {orderType === "limit" && (
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-text-secondary">Limit Price</label>
              <Input type="number" min="0" value={limitPrice} onChange={(e) => setLimitPrice(e.target.value)} className="w-28" />
            </div>
          )}
        </div>

        <label className="flex items-start gap-2 text-sm text-text-secondary">
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

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={() => fireMutation.mutate()} disabled={!canSubmit || fireMutation.isPending}>
            {fireMutation.isPending ? "Placing..." : "Fire Order"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
