"""OHLC4 Buy 5 MIN's chart under its card (27 Nov): the latest session's
finished 5-minute NIFTY candles, and at each one's close the level the
strategy checked it against -- the latest 15-minute NIFTY candle finished
by then, its (O+H+L+C)/4 and its low / high (the stops) -- with the side
that check picks. The level now (for the forming candle) and live NIFTY
while the market is open. Read-only: the stored candles, as the strategy
reads them (native_strategies/nifty_ohlc4_buy.py)."""

from datetime import datetime

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import IST
from app.services.market_data.bar_periods import load_closed_candles
from app.services.market_data.hours import nse_market_open
from app.services.market_data.tick_engine import tick_engine
from app.services.strategy.native_strategies import nifty_ohlc4_buy as ob

NAME = "OHLC4 Buy 5 MIN"
FIVE_FETCH = 160  # two sessions' 5-minute candles and more
LEVEL_FETCH = 60  # the 15-minute ones over the same span, the session before's last included


def is_charted(strategy_name: str | None) -> bool:
    return (strategy_name or "").strip().lower() == NAME.lower()


def _level(bar: dict) -> dict:
    start = as_aware_utc(bar["ts"]).astimezone(IST)
    return {"level": round(ob.level_of(bar), 2), "level_low": float(bar["low"]), "level_high": float(bar["high"]),
            "level_candle": start.isoformat()}


async def ohlc4_series(db, now: datetime) -> dict:
    empty = {"session": None, "spot": None, "level_now": None, "candles": []}
    nifty = (await db.execute(select(Instrument).where(Instrument.symbol == ob.UNDERLYING_SYMBOL))).scalar_one_or_none()
    if nifty is None:
        return empty
    five = await load_closed_candles(db, nifty.id, ob.TIMEFRAME, FIVE_FETCH, now)
    fifteen = await load_closed_candles(db, nifty.id, ob.LEVEL_TIMEFRAME, LEVEL_FETCH, now)
    if not five:
        return empty
    session = as_aware_utc(five[-1]["ts"]).astimezone(IST).date()
    level_ends = [(as_aware_utc(b["ts"]) + ob.LEVEL_BAR, b) for b in fifteen]

    def level_by(moment: datetime) -> dict | None:
        found = None
        for end, bar in level_ends:  # oldest first: the last ended by `moment`
            if end <= moment:
                found = bar
            else:
                break
        return found

    candles = []
    for bar in five:
        start = as_aware_utc(bar["ts"])
        if start.astimezone(IST).date() != session:
            continue
        close_at = start + ob.BAR
        out = {"at": start, "open": float(bar["open"]), "high": float(bar["high"]), "low": float(bar["low"]),
               "close": float(bar["close"]), "level": None, "level_low": None, "level_high": None, "level_candle": None,
               "side": None, "checked": close_at.astimezone(IST).time() <= ob.LAST_DECISION}
        level = level_by(close_at)
        if level is not None:
            out.update(_level(level))
            if out["checked"]:
                out["side"] = ob.wanted_side(out["close"], out["level"])
        candles.append(out)
    latest = level_by(as_aware_utc(now))
    open_now = nse_market_open(now)
    spot = tick_engine.get_current_price(nifty.id) if open_now else None
    return {
        "session": session,
        "spot": float(spot) if spot is not None else None,
        "level_now": _level(latest) if latest is not None and open_now else None,
        "candles": candles,
    }

