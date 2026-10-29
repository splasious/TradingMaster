"""A stock-holding card's P&L by trading day, after charges (29 Oct).

What the card made or lost on a day: each position from the previous
session's close -- or its buy price, when bought that day -- to the day's
close -- or its sale price, when sold that day; the price shown now
(live, or the session close while NSE is shut) for the newest day --
less the estimated charges of the orders placed that day: a buy's on the
day it was bought, a sale's on the day it was sold (trade_record.
order_charges). Over a trade's days these add up to the trade's own P&L
and charges. Before, the card's "Today's Gain" was the whole P&L since
purchase of everything held plus that of everything sold today, before
charges -- not comparable from one day to the next.

Only for a card whose holdings and trades are all NSE/BSE stocks (state
"holdings": MACD - RSI - 15 MIN, the RS Rotations, 52 Week High Breakout);
None otherwise. Display only: nothing here moves the pool's cash or feeds
a strategy.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.paper_trading import PaperNativeDeployment, PaperNativeTrade
from app.services.backfill_platform.coverage import (
    IST,
    is_trading_day,
    ist_date,
    previous_trading_day,
    session_close,
)
from app.services.market_data.closing_price import display_prices, session_closes
from app.services.paper_trading.trade_record import order_charges

STOCKS = ("NSE", "BSE")


@dataclass(frozen=True)
class DayPnl:
    session: date
    gross: float
    charges: float
    net: float
    missing: int  # positions with no close to measure from: left out of gross


@dataclass(frozen=True)
class CardPnl:
    today: DayPnl  # the newest session: today on a trading day, else the last one
    previous: DayPnl  # the session before it
    realized_charges: float  # the pool's closed trades' estimated charges
    open_charges: float  # the buy charges of what the card holds now


@dataclass(frozen=True)
class _Position:
    instrument_id: uuid.UUID
    leg: dict  # quantity, side, exchange, instrument_type: what order_charges reads
    entry_price: float
    opened: date
    closed: date | None
    exit_price: float | None
    held: bool  # still held: priced at the shown price on the newest day


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=IST).astimezone(timezone.utc)


def _is_stock(leg: dict) -> bool:
    return leg.get("exchange") in STOCKS and leg.get("instrument_type") == "equity"


def _short(leg: dict) -> bool:
    return str(leg.get("side", "")).lower() in ("short", "sell")


def day_figures(positions: list[_Position], day: date, start_closes: dict, end_prices: dict) -> DayPnl:
    """`day`'s P&L of `positions`: start_closes -- the previous session's
    close by instrument; end_prices -- the day's close (the shown price on
    the newest day) by instrument."""
    gross = charges = 0.0
    missing = 0
    for p in positions:
        if p.opened > day or (p.closed is not None and p.closed < day):
            continue
        start = p.entry_price if p.opened == day else start_closes.get(p.instrument_id)
        end = p.exit_price if p.closed == day else end_prices.get(p.instrument_id)
        intraday = p.closed is not None and p.opened == p.closed
        if p.opened == day:
            charges += order_charges(p.leg, p.entry_price, not _short(p.leg), intraday) or 0.0
        if p.closed == day:
            charges += order_charges(p.leg, p.exit_price, _short(p.leg), intraday) or 0.0
        if start is None or end is None:
            missing += 1
            continue
        gross += (start - end if _short(p.leg) else end - start) * abs(float(p.leg["quantity"]))
    return DayPnl(day, round(gross, 2), round(charges, 2), round(gross - charges, 2), missing)


async def _closes_of(db: AsyncSession, ids: list[uuid.UUID], day: date) -> dict[uuid.UUID, float]:
    """`day`'s close by instrument -- only where that day's own is saved."""
    closes = await session_closes(db, ids, session_close(day) + timedelta(seconds=1))
    return {i: c.price for i, c in closes.items() if c.session == day}


async def card_pnl(db: AsyncSession, deployment: PaperNativeDeployment, now: datetime | None = None) -> CardPnl | None:
    now = now or datetime.now(timezone.utc)
    holdings = (deployment.state or {}).get("holdings")
    if not isinstance(holdings, dict):
        return None
    today = ist_date(now)
    newest = today if is_trading_day(today) else previous_trading_day(today)
    before = previous_trading_day(newest)

    held_ids = {uuid.UUID(leg["instrument_id"]) for leg in holdings.values()}
    instruments = {
        i.id: i for i in (await db.execute(select(Instrument).where(Instrument.id.in_(held_ids)))).scalars()
    } if held_ids else {}
    positions: list[_Position] = []
    for leg in holdings.values():
        instrument = instruments.get(uuid.UUID(leg["instrument_id"]))
        if instrument is None:
            continue
        spec = {"quantity": leg["quantity"], "side": "long", "exchange": instrument.exchange, "instrument_type": instrument.instrument_type}
        if not _is_stock(spec):
            return None
        opened = datetime.fromisoformat(leg["opened_at"]) if leg.get("opened_at") else now
        positions.append(_Position(instrument.id, spec, float(leg["entry_price"]), ist_date(opened), None, None, True))

    trades = (
        await db.execute(
            select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == deployment.id, PaperNativeTrade.closed_at >= _midnight(before))
        )
    ).scalars().all()
    for trade in trades:
        for leg in trade.legs or []:
            if not _is_stock(leg) or leg.get("instrument_id") is None:
                return None
            positions.append(_Position(
                uuid.UUID(str(leg["instrument_id"])), leg, float(leg["entry_price"]), ist_date(trade.opened_at), ist_date(trade.closed_at),
                float(leg["exit_price"]), False,
            ))

    ids = list({p.instrument_id for p in positions})
    close_before = await _closes_of(db, ids, previous_trading_day(before))
    close_of_before = await _closes_of(db, ids, before)
    shown = {i: price for i, (price, _) in (await display_prices(db, list(held_ids), now)).items() if price is not None}
    previous = day_figures(positions, before, close_before, close_of_before)
    latest = day_figures(positions, newest, close_of_before, shown)

    pool_trades = (
        select(PaperNativeTrade.id).join(PaperNativeDeployment, PaperNativeDeployment.id == PaperNativeTrade.deployment_id)
        .where(PaperNativeDeployment.portfolio_id == deployment.portfolio_id)
    )
    realized_charges = (
        await db.execute(select(func.coalesce(func.sum(PaperNativeTrade.charges), 0.0)).where(PaperNativeTrade.id.in_(pool_trades)))
    ).scalar_one()
    open_charges = sum(order_charges(p.leg, p.entry_price, True, False) or 0.0 for p in positions if p.held)
    return CardPnl(latest, previous, round(float(realized_charges or 0.0), 2), round(open_charges, 2))
