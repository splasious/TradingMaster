"""Where a native strategy's live orders meet a broker (native_live.py).

One small, broker-agnostic surface on top of BrokerInterface:

  market_order()  place a market order and wait for the broker to report
                  it done -- filled (how much, at what average price),
                  rejected or cancelled. One still working after
                  FILL_TIMEOUT_SECONDS is cancelled and counts as failed.
  net_positions() what the account holds now, keyed by (exchange,
                  tradingsymbol), for reconciliation.

The NSE brokers this app trades through -- Zerodha, Kotak Neo, HDFC
Securities -- share Kite's order vocabulary (tradingsymbol / exchange /
product, oms.get_live_price_and_context); each adapter's place_order()
translates from there. Angel One and Dhan are connect-only for now:
oms.get_authenticated_broker refuses them before a gateway exists.

Product by the strategy's style (agreed 1 Oct): intraday -> MIS; overnight
-> NRML for F&O, CNC for stocks.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from app.models.instrument import Instrument
from app.models.live_native import PRODUCT_INTRADAY
from app.services.live_trading.order_state_machine import STATE_MAPS, TERMINAL_STATUSES, LiveOrderStatus

FILL_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.5
FNO_TYPES = ("option", "future")

# The fill fields of each broker's order report, first match wins: Kite
# (also HDFC's assumed shape), then Kotak Neo's.
_FILLED_QTY_KEYS = ("filled_quantity", "fldQty", "filledQty")
_AVG_PRICE_KEYS = ("average_price", "avgPrc", "averagePrice")


@dataclass
class Fill:
    status: LiveOrderStatus
    filled_quantity: float
    average_price: float | None
    broker_order_id: str | None
    reason: str | None = None

    @property
    def complete(self) -> bool:
        return self.status == LiveOrderStatus.FILLED


def _number(raw: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def product_for(instrument: Instrument, product_style: str) -> str:
    if product_style == PRODUCT_INTRADAY:
        return "MIS"
    return "NRML" if instrument.instrument_type in FNO_TYPES else "CNC"


def exchange_for(instrument: Instrument) -> str:
    return "NFO" if instrument.instrument_type in FNO_TYPES else "NSE"


class BrokerGateway:
    def __init__(self, broker_code: str, broker: Any) -> None:
        self.broker_code = broker_code
        self.broker = broker
        self.state_map = STATE_MAPS.get(broker_code, {})

    def key_for(self, instrument: Instrument) -> tuple[str, str]:
        return exchange_for(instrument), instrument.external_ref

    def _read(self, report: dict) -> tuple[LiveOrderStatus, float | None, float | None, str | None]:
        raw = report.get("raw") or {}
        status = self.state_map.get(report.get("status"), LiveOrderStatus.OPEN)
        reason = raw.get("status_message") or raw.get("rejRsn") or raw.get("reason")
        return status, _number(raw, _FILLED_QTY_KEYS), _number(raw, _AVG_PRICE_KEYS), reason

    async def market_order(self, instrument: Instrument, side: str, quantity: float, product: str, client_order_id: str) -> Fill:
        order = {
            "tradingsymbol": instrument.external_ref, "exchange": exchange_for(instrument), "product": product,
            "quantity": quantity, "side": side, "order_type": "market", "client_order_id": client_order_id,
        }
        try:
            placement = await self.broker.place_order(order)
        except Exception as exc:  # a refusal at the door: rejected, nothing filled
            return Fill(LiveOrderStatus.REJECTED, 0.0, None, None, str(exc)[:500])
        order_id = str(placement.get("broker_order_id") or "")
        if not order_id:
            return Fill(LiveOrderStatus.REJECTED, 0.0, None, None, str(placement.get("reason") or "no order id returned")[:500])

        deadline = time.monotonic() + FILL_TIMEOUT_SECONDS
        last = (LiveOrderStatus.SUBMITTED, None, None, None)
        while True:
            try:
                last = self._read(await self.broker.get_order_status(order_id))
            except Exception as exc:
                last = (last[0], last[1], last[2], f"status check failed: {exc}"[:500])
            status, filled, avg, reason = last
            if status in TERMINAL_STATUSES:
                if status == LiveOrderStatus.FILLED and filled is None:
                    filled = quantity
                return Fill(status, filled or 0.0, avg, order_id, reason)
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_SECONDS)

        # Still working: cancel it, then take what filled before the cancel.
        try:
            await self.broker.cancel_order(order_id)
        except Exception:
            pass
        try:
            status, filled, avg, reason = self._read(await self.broker.get_order_status(order_id))
        except Exception:
            status, filled, avg, reason = last
        if status == LiveOrderStatus.FILLED:  # filled just as it was cancelled
            return Fill(status, filled or quantity, avg, order_id, reason)
        return Fill(LiveOrderStatus.CANCELLED, filled or 0.0, avg, order_id,
                    f"not filled within {FILL_TIMEOUT_SECONDS:.0f} s -- cancelled" + (f" ({reason})" if reason else ""))

    async def net_positions(self) -> dict[tuple[str, str], float]:
        """Net quantity per (exchange, tradingsymbol): the day's and carried
        positions, plus delivery holdings where the adapter can list them
        (a stock bought CNC moves from positions to holdings overnight)."""
        held: dict[tuple[str, str], float] = {}
        for row in await self.broker.get_positions() or []:
            key = (str(row.get("exchange") or ""), str(row.get("tradingsymbol") or ""))
            held[key] = held.get(key, 0.0) + float(row.get("quantity") or 0)
        get_holdings = getattr(self.broker, "get_holdings", None)
        if callable(get_holdings):
            for row in await get_holdings() or []:
                key = (str(row.get("exchange") or "NSE"), str(row.get("tradingsymbol") or ""))
                held[key] = held.get(key, 0.0) + float(row.get("quantity") or 0) + float(row.get("t1_quantity") or 0)
        return held
