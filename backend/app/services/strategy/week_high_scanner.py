"""52 Week High Breakout's "next in line" (20 Nov): the stocks its Friday
check would buy next, for the panel under the card's holdings.

Read-only: it trades nothing and leaves the strategy's state alone. Agreed
20 Nov -- of the strategy's 500 stocks, those not held:

  breakouts first  live price above the highest weekly high of the 52
                   completed weeks before this week -- what the check at
                   Friday 15:00 buys, while a slot is free -- strongest %
                   first, the order the check buys in;
  then             the rest, nearest below that level first.

LIMIT (6) of them. The levels are the strategy's own (week_high_breakout_52:
its weekly candles from the stored daily ones), worked out once a day and
kept (_LEVELS); the prices one Kite quote call for all of them, kept
QUOTE_TTL (60 s) and shared by every viewer. Without a Kite login, the last
stored day's close stands in, and the panel says so.
"""

import time as _clock
import types
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.models.instrument import Instrument
from app.services.strategy.native_strategies import week_high_breakout_52 as whb

NAME = "52 Week High Breakout"
LIMIT = 6
QUOTE_TTL = 60.0  # seconds a quote call is kept

_LEVELS: dict[date, dict[uuid.UUID, tuple[float, float]]] = {}  # today -> {instrument id: (52-week level, last stored close)}
_QUOTES: dict[str, object] = {}  # "at" (monotonic), "prices" {instrument id: price}, "why" -- the last quote call


def is_scanned(strategy_name: str | None) -> bool:
    return (strategy_name or "").strip().lower() == NAME.lower()


async def _universe(db) -> list[Instrument]:
    return (
        await db.execute(select(Instrument).where(Instrument.exchange == "NSE", Instrument.symbol.in_(whb.STOCK_UNIVERSE)))
    ).scalars().all()


async def levels(db, today: date, instruments: list[Instrument]) -> dict[uuid.UUID, tuple[float, float]]:
    """{instrument id: (the highest weekly high of the BREAKOUT_CANDLES completed
    weeks before this week, the last stored close)} -- once a day."""
    if today in _LEVELS:
        return _LEVELS[today]
    this_week = whb.monday(today)
    days = await whb._days(types.SimpleNamespace(db=db), [i.id for i in instruments],
                           this_week - timedelta(weeks=whb.HISTORY_WEEKS), today)
    out = {}
    for instrument_id, bars in days.items():
        finished = [w for w in whb.weeks([d for d in bars if d["day"] < this_week]) if w["week"] < this_week]
        if len(finished) < whb.BREAKOUT_CANDLES:
            continue
        out[instrument_id] = (max(w["high"] for w in finished[-whb.BREAKOUT_CANDLES:]), bars[-1]["close"])
    _LEVELS.clear()
    _LEVELS[today] = out
    return out


async def prices(db, instruments: list[Instrument]) -> tuple[dict[uuid.UUID, float], str | None]:
    """Live prices from one Kite quote call (kept QUOTE_TTL), and why there are none."""
    if _QUOTES and _clock.monotonic() - float(_QUOTES["at"]) < QUOTE_TTL:
        return _QUOTES["prices"], _QUOTES["why"]  # type: ignore[return-value]
    out: dict[uuid.UUID, float] = {}
    why = None
    creds = await whb.find_connected_zerodha_credentials(db)
    if creds is None:
        why = "Zerodha isn't logged in"
    else:
        broker = whb.ZerodhaKiteBroker()
        broker._api_key, broker._access_token = creds["api_key"], creds["access_token"]
        keys = {f"{i.exchange}:{i.external_ref}": i.id for i in instruments}
        quotes, error = await whb.kite_quotes(broker, list(keys))
        for key, instrument_id in keys.items():
            price = (quotes.get(key) or {}).get("last_price")
            if price:
                out[instrument_id] = float(price)
        if not out:
            why = f"no Kite quotes ({error or 'none returned'})"
    _QUOTES.update(at=_clock.monotonic(), prices=out, why=why)
    return out, why


async def next_in_line(db, holdings: dict, now: datetime) -> dict:
    """The panel: slots, the next check, and up to LIMIT stocks in the order the check would take them."""
    now_ist = now.astimezone(whb.IST)
    instruments = await _universe(db)
    level_of = await levels(db, now_ist.date(), instruments)
    live, why = await prices(db, instruments)
    held = set(holdings or {})
    slots_free = max(whb.MAX_POSITIONS - len(held), 0)
    rows = []
    for instrument in instruments:
        if instrument.symbol in held or instrument.id not in level_of:
            continue
        level, last_close = level_of[instrument.id]
        price = live.get(instrument.id, last_close if not live else None)
        if price is None or level <= 0:
            continue
        rows.append({"symbol": instrument.symbol, "price": round(price, 2), "level": round(level, 2),
                     "gap_pct": round((price / level - 1) * 100, 2), "breakout": price > level})
    rows.sort(key=lambda r: (not r["breakout"], -r["gap_pct"]))  # breakouts strongest first, then the nearest below
    return {
        "slots_total": whb.MAX_POSITIONS, "slots_free": slots_free, "next_check": whb._next_check(now_ist),
        "as_of": now.astimezone(timezone.utc), "price_source": "close" if not live else "live",
        "note": f"{why}: prices are the last stored close" if not live and why else why, "rows": rows[:LIMIT],
        "breakouts_now": sum(1 for r in rows if r["breakout"]),
    }
