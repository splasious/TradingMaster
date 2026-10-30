"""52 Week High Breakout -- its open positions at last Friday's 3 PM (30 Oct).

A one-off look, no trades: each stock the card holds, as the Friday 3 PM
check (version 3) would have seen it on CHECK_DAY at DECIDE_AT -- that
week's candle from its stored days plus the check day's 5-minute candles
from Kite up to 15:00, its close the price at 15:00 -- against its 10-week
MA of (O+H+L+C)/4 (sold below it, kept above), with how far it stood over
its 52-week high.

Read-only and self-contained -- the server may still run version 2, so
the rules are the version-3 ones written out here, not imported. Run
inside the backend container (the health check's `friday_check` input
pipes it to `python -`). Prices and names go to the
PRIVATE Telegram chat only; this repository and its Actions log are
public, so the log gets counts alone.
"""

import asyncio
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import PaperNativeDeployment
from app.models.strategy import StrategyVersion
from app.services.backfill_platform.coverage import is_trading_day, previous_trading_day
from app.services.broker.kite_ticker_service import find_connected_zerodha_credentials
from app.services.broker.zerodha_broker import IST, ZerodhaKiteBroker
from app.services.fo_scan.pacing import kite_history
from app.services.notifications.telegram import send_telegram

CHECK_DAY = date(2026, 10, 23)  # last Friday
DECIDE_AT, SESSION_OPEN = time(15, 0), time(9, 15)
BREAKOUT_CANDLES, MA_CANDLES, HISTORY_WEEKS = 52, 10, 56
CODE = "%52 Week High Breakout -- 500 NSE stocks on weekly candles%"


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def weeks(days: list[dict]) -> list[dict]:
    out: list[dict] = []
    for bar in days:
        week = monday(bar["day"])
        if out and out[-1]["week"] == week:
            last = out[-1]
            last["high"], last["low"] = max(last["high"], bar["high"]), min(last["low"], bar["low"])
            last["close"], last["last_day"] = bar["close"], bar["day"]
        else:
            out.append({"week": week, "last_day": bar["day"], **{k: bar[k] for k in ("open", "high", "low", "close")}})
    return out


def week_so_far(days: list[dict], today_bar: dict | None, this_week: date, today: date) -> list[dict] | None:
    """As week_high_breakout_52.week_so_far (version 3)."""
    finished = weeks([d for d in days if d["day"] < this_week])
    during = [d for d in days if this_week <= d["day"] < today]
    if not finished or finished[-1]["week"] != this_week - timedelta(weeks=1) or finished[-1]["last_day"] != previous_trading_day(this_week):
        return None
    expected = [this_week + timedelta(days=k) for k in range((today - this_week).days) if is_trading_day(this_week + timedelta(days=k))]
    if [d["day"] for d in during] != expected or today_bar is None:
        return None
    return finished + weeks(during + [today_bar])


async def stored_days(db, instrument_id, start: date, end: date) -> list[dict]:
    rows = await db.execute(
        select(OhlcvCandle.ts, OhlcvCandle.open, OhlcvCandle.high, OhlcvCandle.low, OhlcvCandle.close)
        .where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.timeframe == "1d",
               OhlcvCandle.ts >= datetime.combine(start, time(0), tzinfo=IST), OhlcvCandle.ts < datetime.combine(end, time(0), tzinfo=IST))
        .order_by(OhlcvCandle.ts)
    )
    out: list[dict] = []
    for ts, o, h, lo, c in rows:
        day = as_aware_utc(ts).astimezone(IST).date()
        if None in (o, h, lo, c) or (out and out[-1]["day"] == day):
            continue
        out.append({"day": day, "open": float(o), "high": float(h), "low": float(lo), "close": float(c)})
    return out


def day_until(bars: list[dict], day: date, until: datetime) -> dict | None:
    """The day's bar from its 5-minute candles finished by `until`."""
    done = [b for b in bars if as_aware_utc(b["ts"]) + timedelta(minutes=5) <= until]
    if not done:
        return None
    return {"day": day, "open": float(done[0]["open"]), "high": max(float(b["high"]) for b in done),
            "low": min(float(b["low"]) for b in done), "close": float(done[-1]["close"])}


def verdict(bars: list[dict]) -> dict:
    ma = sum((b["open"] + b["high"] + b["low"] + b["close"]) / 4 for b in bars[-MA_CANDLES:]) / MA_CANDLES if len(bars) >= MA_CANDLES else None
    close = bars[-1]["close"]
    high = max(b["high"] for b in bars[-BREAKOUT_CANDLES - 1:-1]) if len(bars) > BREAKOUT_CANDLES else None
    return {"close": close, "ma": ma, "keep": ma is None or close >= ma, "over_high": (close / high - 1) * 100 if high else None}


async def main() -> None:
    this_week = monday(CHECK_DAY)
    until = datetime.combine(CHECK_DAY, DECIDE_AT, tzinfo=IST)
    lines, kept, sold, unknown = [], 0, 0, 0
    async with AsyncSessionLocal() as db:
        cards = (
            await db.execute(
                select(PaperNativeDeployment).join(StrategyVersion, StrategyVersion.id == PaperNativeDeployment.strategy_version_id)
                .where(StrategyVersion.python_code.like(CODE), PaperNativeDeployment.status.in_(("active", "paused")))
            )
        ).scalars().all()
        creds = await find_connected_zerodha_credentials(db)
        broker = None
        if creds is not None:
            broker = ZerodhaKiteBroker()
            broker._api_key, broker._access_token = creds["api_key"], creds["access_token"]
        for card in cards:
            for symbol, leg in ((card.state or {}).get("holdings") or {}).items():
                instrument = await db.get(Instrument, uuid.UUID(leg["instrument_id"]))
                days = await stored_days(db, instrument.id, this_week - timedelta(weeks=HISTORY_WEEKS), CHECK_DAY)
                friday = None
                if broker is not None:
                    try:
                        five = await kite_history(broker, instrument.external_ref, "5m", datetime.combine(CHECK_DAY, SESSION_OPEN, tzinfo=IST),
                                                  until, instrument.exchange)
                        friday = day_until(five, CHECK_DAY, until)
                    except Exception as exc:  # noqa: BLE001 -- reported, the rest still checked
                        lines.append(f"{symbol}: Kite's 5-minute candles failed ({type(exc).__name__})")
                bars = week_so_far(days, friday, this_week, CHECK_DAY)
                bought = f"bought {datetime.fromisoformat(leg['opened_at']).astimezone(IST):%d-%b %H:%M} at {leg['entry_price']:,.2f}"
                if bars is None:
                    unknown += 1
                    lines.append(f"{symbol}: not enough data for the week to {CHECK_DAY:%d-%b} at 3 PM ({bought})")
                    continue
                v = verdict(bars)
                kept, sold = kept + v["keep"], sold + (not v["keep"])
                over = f", {v['over_high']:+.2f}% vs its 52-week high" if v["over_high"] is not None else ""
                ma = f"{v['ma']:,.2f}" if v["ma"] is not None else "n/a"
                lines.append(f"{symbol}: 3 PM {v['close']:,.2f} vs 10-week MA {ma} -> {'kept' if v['keep'] else 'would be sold'}{over} ({bought})")
    text = "\n".join(lines) or "No open positions on the card."
    await send_telegram(f"52 Week High Breakout · open positions at last Friday's 3 PM ({CHECK_DAY:%a %d %b} 15:00)", text)
    print(f"friday check: {kept + sold + unknown} positions -- {kept} above their 10-week MA, {sold} below, {unknown} without data; "
          f"sent to the private chat")


if __name__ == "__main__":
    asyncio.run(main())
