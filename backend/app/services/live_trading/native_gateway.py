"""Where a native strategy's live orders meet a broker (native_live.py).

One small, broker-agnostic surface on top of BrokerInterface:

  order()         a protected market order: a LIMIT priced
                  MARKET_PROTECTION_PCT past the current price (buy above,
                  sell below), then wait for the broker to report it done
                  -- filled (how much, at what average price), rejected or
                  cancelled. One still working after FILL_TIMEOUT_SECONDS
                  is cancelled and counts as failed.
  net_positions() what the account holds now, keyed the way the broker
                  identifies each contract (key()), for reconciliation.

Plain market orders aren't accepted through broker APIs any more (since
April 2026 Zerodha rejects one without market protection, and Angel One
doesn't allow them for algos), so every order is a limit a step past the
price -- it fills like a market order, at the best price available, but
never further away than the step (agreed 1 Oct 2026: 1%, at least one
price step). Prices go on a grid every NSE tick size divides (price_step):
0.05 for options, 0.10 for futures, and for stocks 0.10 up to Rs 5,000,
1 up to Rs 20,000, 5 above.

The contract each order names is the broker's own (broker_contracts.py):
Zerodha's symbols are this app's; Dhan and Angel One are matched by what
the contract is. Each adapter's place_order() takes the same order dict --
tradingsymbol / exchange / token / product (Kite's MIS-NRML-CNC) /
order_type / limit_price -- and translates it to its broker's vocabulary.
Kotak Neo and HDFC Securities aren't wired for live strategies yet
(registry.supports_live_strategies).

Product by the strategy's style (agreed 1 Oct): intraday -> MIS; overnight
-> NRML for F&O, CNC for stocks.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from app.models.instrument import Instrument
from app.models.live_native import PRODUCT_INTRADAY
from app.services.backfill_platform.coverage import IST
from app.services.broker.registry import supports_live_strategies
from app.services.live_trading import broker_contracts
from app.services.live_trading.broker_contracts import BrokerContract, ContractListError, ContractNotFound
from app.services.live_trading.order_state_machine import STATE_MAPS, TERMINAL_STATUSES, LiveOrderStatus

FILL_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.5
MARKET_PROTECTION_PCT = 1.0
FNO_TYPES = ("option", "future")

# How often an order's status is asked for while it works: Angel One reads
# it from the whole order book, so less often.
_POLL_SECONDS = {"angel_one": 1.0}

# The fill fields of each broker's order report, first match wins: Kite,
# Kotak Neo, Dhan, Angel One.
_FILLED_QTY_KEYS = ("filled_quantity", "fldQty", "filledQty", "filledshares")
_AVG_PRICE_KEYS = ("average_price", "avgPrc", "averageTradedPrice", "averageprice")
_REASON_KEYS = ("status_message", "rejRsn", "omsErrorDescription", "text", "reason")


@dataclass
class Fill:
    status: LiveOrderStatus
    filled_quantity: float
    average_price: float | None
    broker_order_id: str | None
    reason: str | None = None
    limit_price: float | None = None

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


def price_step(instrument: Instrument, price: float) -> Decimal:
    if instrument.instrument_type == "option":
        return Decimal("0.05")
    if instrument.instrument_type == "future":
        return Decimal("0.10")
    if price < 5000:
        return Decimal("0.10")
    if price < 20000:
        return Decimal("1")
    return Decimal("5")


def protected_limit(instrument: Instrument, side: str, price: float, pct: float = MARKET_PROTECTION_PCT) -> float:
    """The limit MARKET_PROTECTION_PCT past `price` -- buying above, selling
    below -- on the price grid, and at least one step past it."""
    step = price_step(instrument, price)
    ref = Decimal(str(price))
    if side == "buy":
        limit = (ref * (1 + Decimal(str(pct)) / 100) / step).to_integral_value(ROUND_CEILING) * step
        limit = max(limit, (ref / step).to_integral_value(ROUND_FLOOR) * step + step)
    else:
        limit = (ref * (1 - Decimal(str(pct)) / 100) / step).to_integral_value(ROUND_FLOOR) * step
        limit = min(limit, (ref / step).to_integral_value(ROUND_CEILING) * step - step)
        limit = max(limit, step)  # never zero or below
    return float(limit)


def _today() -> Any:
    return datetime.now(IST).date()


# What an account holds, per broker: (rows' key fields, quantity field).
def _kite_holdings(rows: list[dict]) -> dict:
    out: dict = {}
    for row in rows:
        key = (str(row.get("exchange") or "NSE"), str(row.get("tradingsymbol") or ""))
        out[key] = out.get(key, 0.0) + float(row.get("quantity") or 0) + float(row.get("t1_quantity") or 0)
    return out


_POSITION_KEYS = {
    "zerodha_kite": (("exchange", "tradingsymbol"), ("quantity",)),
    "dhan": (("exchangeSegment", "securityId"), ("netQty",)),
    "angel_one": (("exchange", "symboltoken"), ("netqty",)),
}


def _holdings(broker_code: str, rows: list[dict]) -> dict:
    if broker_code == "zerodha_kite":
        return _kite_holdings(rows)
    out: dict = {}
    for row in rows:
        if broker_code == "dhan":  # totalQty: in the demat account plus T1
            key, quantity = ("NSE_EQ", str(row.get("securityId") or "")), float(row.get("totalQty") or 0)
        elif broker_code == "angel_one":
            key = (str(row.get("exchange") or "NSE"), str(row.get("symboltoken") or ""))
            quantity = float(row.get("quantity") or 0) + float(row.get("t1quantity") or 0)
        else:
            continue
        out[key] = out.get(key, 0.0) + quantity
    return out


class BrokerGateway:
    def __init__(self, broker_code: str, broker: Any) -> None:
        self.broker_code = broker_code
        self.broker = broker
        self.state_map = STATE_MAPS.get(broker_code, {})

    async def contract(self, instrument: Instrument) -> BrokerContract:
        if not supports_live_strategies(self.broker_code):
            raise ContractNotFound(f"live strategies can't trade through {self.broker_code} yet")
        return await broker_contracts.contract_for(self.broker_code, self.broker, instrument, _today())

    async def key(self, instrument: Instrument) -> tuple[str, str]:
        return (await self.contract(instrument)).key

    def _read(self, report: dict) -> tuple[LiveOrderStatus, float | None, float | None, str | None]:
        raw = report.get("raw") or {}
        status = self.state_map.get(report.get("status"), LiveOrderStatus.OPEN)
        reason = next((str(raw[k]) for k in _REASON_KEYS if raw.get(k)), None)
        return status, _number(raw, _FILLED_QTY_KEYS), _number(raw, _AVG_PRICE_KEYS), reason

    async def order(self, instrument: Instrument, side: str, quantity: float, product: str, client_order_id: str,
                    price: float) -> Fill:
        try:
            contract = await self.contract(instrument)
        except (ContractNotFound, ContractListError) as exc:
            return Fill(LiveOrderStatus.REJECTED, 0.0, None, None, f"not sent: {exc}"[:500])
        limit = protected_limit(instrument, side, price)
        order = {
            "tradingsymbol": contract.symbol, "exchange": contract.exchange, "token": contract.token, "product": product,
            "quantity": quantity, "side": side, "order_type": "limit", "limit_price": limit, "client_order_id": client_order_id,
        }
        try:
            placement = await self.broker.place_order(order)
        except Exception as exc:  # a refusal at the door: rejected, nothing filled
            return Fill(LiveOrderStatus.REJECTED, 0.0, None, None, str(exc)[:500], limit)
        order_id = str(placement.get("broker_order_id") or "")
        if not order_id:
            return Fill(LiveOrderStatus.REJECTED, 0.0, None, None, str(placement.get("reason") or "no order id returned")[:500], limit)

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
                return Fill(status, filled or 0.0, avg, order_id, reason, limit)
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_POLL_SECONDS.get(self.broker_code, POLL_SECONDS) if POLL_SECONDS else 0)

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
            return Fill(status, filled or quantity, avg, order_id, reason, limit)
        return Fill(LiveOrderStatus.CANCELLED, filled or 0.0, avg, order_id,
                    f"not filled within {FILL_TIMEOUT_SECONDS:.0f} s at {limit:g} or better -- cancelled"
                    + (f" ({reason})" if reason else ""), limit)

    async def net_positions(self) -> dict[tuple[str, str], float]:
        """Net quantity per contract key: the day's and carried positions,
        plus delivery holdings (a stock bought CNC moves from positions to
        holdings overnight)."""
        fields, quantity_fields = _POSITION_KEYS.get(self.broker_code, (("exchange", "tradingsymbol"), ("quantity",)))
        held: dict[tuple[str, str], float] = {}
        for row in await self.broker.get_positions() or []:
            key = tuple(str(row.get(f) or "") for f in fields)
            held[key] = held.get(key, 0.0) + float(_number(row, quantity_fields) or 0)
        get_holdings = getattr(self.broker, "get_holdings", None)
        if callable(get_holdings):
            for key, quantity in _holdings(self.broker_code, await get_holdings() or []).items():
                held[key] = held.get(key, 0.0) + quantity
        return held
