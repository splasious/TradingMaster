"""Before a Conti Straddle duplicate with monthly buys (4 Nov): read-only facts.

- Conti Straddle 15 MIN: its versions, whether the newest one's code is the
  built-in file's (md5 only, "\r" stripped), its paper card(s) by status;
- NIFTY option expiries the app has contracts for, from today: how many
  contracts each, their strike range, lot size, and which is a month's last
  (the monthly);
- whether the running app streams those expiries: a few contracts near
  NIFTY of each expiry, over the market-data WebSocket for a few seconds.

Run inside the backend container (the health check's `conti_calendar_check`
input pipes it to `python -`). The public log gets counts, dates, md5s and
yes/no only -- no code, no user, no P&L.
"""

import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

import websockets
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.instrument import Instrument
from app.models.paper_trading import PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine  # noqa: F401 -- not this process's; ticks come over the WebSocket

NAMES = {"Conti Straddle 15 MIN": "03536e1f5a51afd155c25f286b3da832", "Conti Calendar 15 MIN": "8eccd7bd20441d55227be3a015d730ea",
         "Conti Calendar 400 15 MIN": "5226c6668e3ad16736f8c0bb48474ffc",
         "Conti Reverse Calendar 15 MIN": "05ad04b64bb69e9d604e579ffac1ba09",
         "OI Change Buy 15 MIN": "758bf271f4c2f171b3abf3b6b5843cd0"}
LISTEN_SECONDS = 6


def md5(code: str | None) -> str:
    return hashlib.md5((code or "").replace("\r", "").encode()).hexdigest()


async def describe(db, NAME: str, FILE_MD5: str) -> None:
    strategies = (await db.execute(select(Strategy).where(func.trim(Strategy.name) == NAME))).scalars().all()
    print(f"strategies named {NAME!r}: {len(strategies)}")
    for s in strategies:
        versions = (await db.execute(
            select(StrategyVersion).where(StrategyVersion.strategy_id == s.id).order_by(StrategyVersion.version_number)
        )).scalars().all()
        cards = (await db.execute(
            select(PaperNativeDeployment.status, PaperNativeDeployment.strategy_version_id, func.count())
            .where(PaperNativeDeployment.strategy_id == s.id).group_by(PaperNativeDeployment.status, PaperNativeDeployment.strategy_version_id)
        )).all()
        by_id = {v.id: v.version_number for v in versions}
        print(f"  versions: {', '.join(f'v{v.version_number} {md5(v.python_code)[:8]} {v.timeframe}' for v in versions)}")
        newest = versions[-1] if versions else None
        print(f"  newest is the built-in file: {newest is not None and md5(newest.python_code) == FILE_MD5}")
        print(f"  paper cards: {', '.join(f'{st} on v{by_id.get(vid)} x{n}' for st, vid, n in cards) or 'none'}")
        runs = (await db.execute(select(PaperNativeDeployment).where(PaperNativeDeployment.strategy_id == s.id))).scalars().all()
        for card in runs:
            if card.status == "active" and (card.state or {}).get("position"):
                print(f"  card {str(card.id)[:8]} on the card: {_legs_at_book(card.id, s.owner_id)}")
            ran = card.last_evaluated_at.astimezone(IST).strftime("%d-%b %H:%M:%S") if card.last_evaluated_at else "never"
            print(f"  card {str(card.id)[:8]}: last run {ran}, last signal {card.last_signal or '-'}, "
                  f"position open: {bool((card.state or {}).get('position'))}")
            position = (card.state or {}).get("position")
            if NAME == "Conti Calendar 400 15 MIN":
                if position:
                    opened = datetime.fromisoformat(position["opened_at"]).astimezone(IST).strftime("%d-%b %H:%M:%S")
                    basis = f"ATM {position['atm']:,.0f}" if position.get("atm") is not None else f"weekly bids >= {position.get('min_premium', 100):,.0f}"
                    print(f"    opened {opened}: {position['strike_ce']:,.0f} CE / {position['strike_pe']:,.0f} PE ({basis}), "
                          f"weekly {position['weekly_expiry']}, monthly {position['monthly_expiry']}, rolls {position.get('rolls', 0)}")
                    for name, leg in position["legs"].items():
                        instrument = await db.get(Instrument, uuid.UUID(leg["instrument_id"]))
                        print(f"    {name}: {leg['side']} {instrument.symbol if instrument else '?'} {leg['quantity'] / 65:g} lots at {leg['entry_price']:,.2f}")
                    print(f"    checked at {position.get('checked_at', '-')}; extremes tracked: {position.get('max_profit_reached') is not None}")
                else:
                    print(f"    last reason: {(card.last_signal_reason or '-').split(', P&L')[0].split('; P&L')[0]}")
                closes = (await db.execute(
                    select(PaperNativeTrade.exit_reason, PaperNativeTrade.opened_at, PaperNativeTrade.closed_at, PaperNativeTrade.legs)
                    .where(PaperNativeTrade.deployment_id == card.id).order_by(PaperNativeTrade.closed_at.desc()).limit(5)
                )).all()
                for reason, opened_at, closed_at, legs in closes:  # times, reasons and strikes only
                    strikes = sorted({f"{leg.get('strike', 0):,.0f} {leg.get('option_type', '')}" for leg in legs if leg.get("strike")})
                    print(f"    closed trade: {reason}, {as_aware_utc(opened_at).astimezone(IST):%d-%b %H:%M:%S} -> "
                          f"{as_aware_utc(closed_at).astimezone(IST):%d-%b %H:%M:%S}, {len(legs)} legs {', '.join(strikes)}")
                continue
            if NAME == "OI Change Buy 15 MIN":  # sides, strikes, times and the day's change in OI only -- no P&L
                state = card.state or {}
                oi = state.get("last_oi") or {}
                print(f"    last 15-min close read: {state.get('last_mark', '-')}; change in OI then (15 min / day): "
                      f"put {oi.get('put_change_15m', '-')} / {oi.get('put_change_day', '-')}, "
                      f"call {oi.get('call_change_15m', '-')} / {oi.get('call_change_day', '-')}; buy pending: {bool(state.get('pending'))}")
                if position:
                    opened = datetime.fromisoformat(position["opened_at"]).astimezone(IST).strftime("%d-%b %H:%M:%S")
                    leg = position["legs"]["long"]
                    print(f"    holding {position['option']} ({position['bias']}), bought {opened} at {leg['entry_price']:,.2f}, "
                          f"{leg['quantity'] / 65:g} lot, decided at the {position.get('decided_at')} close")
                print(f"    last reason: {(card.last_signal_reason or '-').split(', P&L')[0].split('; P&L')[0]}")
                closes = (await db.execute(
                    select(PaperNativeTrade.exit_reason, PaperNativeTrade.opened_at, PaperNativeTrade.closed_at, PaperNativeTrade.legs)
                    .where(PaperNativeTrade.deployment_id == card.id).order_by(PaperNativeTrade.closed_at.desc()).limit(8)
                )).all()
                for reason, opened_at, closed_at, legs in closes:
                    held = ", ".join(f"{leg.get('strike', 0):,.0f} {leg.get('option_type', '')}" for leg in legs)
                    print(f"    closed trade: {reason}, {as_aware_utc(opened_at).astimezone(IST):%d-%b %H:%M:%S} -> "
                          f"{as_aware_utc(closed_at).astimezone(IST):%d-%b %H:%M:%S}, {held}")
                from app.models.pcr import PcrSnapshot  # NIFTY market data: today's records, as the card reads them

                today = datetime.now(timezone.utc).astimezone(IST).date()
                snaps = (await db.execute(
                    select(PcrSnapshot).where(PcrSnapshot.underlying == "NIFTY", PcrSnapshot.session_date == today).order_by(PcrSnapshot.ts)
                )).scalars().all()
                print("    today's records (close: day's change put / call -> side | last 15 min put / call -> side; captured after):")
                side = lambda put, call: "-" if put is None or call is None or put == call else ("CALL" if put > call else "PUT")  # noqa: E731
                for snap in snaps:
                    lag = (as_aware_utc(snap.captured_at) - as_aware_utc(snap.ts)).total_seconds()
                    print(f"      {as_aware_utc(snap.ts).astimezone(IST):%H:%M}: day {snap.put_oi_change_day} / {snap.call_oi_change_day} -> "
                          f"{side(snap.put_oi_change_day, snap.call_oi_change_day)} | 15m {snap.put_oi_change} / {snap.call_oi_change} -> "
                          f"{side(snap.put_oi_change, snap.call_oi_change)}; {snap.source} {lag:+.0f}s")
                continue
            if NAME not in ("Conti Calendar 15 MIN", "Conti Reverse Calendar 15 MIN"):
                continue
            if position:
                opened = datetime.fromisoformat(position["opened_at"]).astimezone(IST).strftime("%d-%b %H:%M:%S")
                print(f"    opened {opened}: strike {position['strike']:,.0f} ({position['side']}-heavy, PCR {position['pcr_at_entry']:.3f}), "
                      f"weekly {position['weekly_expiry']}, monthly {position['monthly_expiry']}")
                for name, leg in position["legs"].items():
                    instrument = await db.get(Instrument, uuid.UUID(leg["instrument_id"]))
                    print(f"    {name}: {leg['side']} {instrument.symbol if instrument else '?'} {leg['quantity'] / 65:g} lots at {leg['entry_price']:,.2f}")
                trades = (await db.execute(select(func.count()).select_from(PaperNativeTrade).where(PaperNativeTrade.deployment_id == card.id))).scalar()
                print(f"    closed trades so far: {trades}; take-profit last checked {position.get('checked_at', '-')}")
                seen = []
                for _ in range(4):  # how often it's re-checked: the check time, read once a second
                    await asyncio.sleep(1)
                    await db.refresh(card)
                    seen.append(((card.state or {}).get("position") or {}).get("checked_at", "-"))
                print(f"    re-read every second: {', '.join(seen)}")
            elif card.last_signal != "HOLD":
                reason = (card.last_signal_reason or "-").split(", P&L")[0]  # no P&L in this public log
                print(f"    last reason: {reason}")
            last = (await db.execute(
                select(PaperNativeTrade.exit_reason, PaperNativeTrade.closed_at, PaperNativeTrade.legs)
                .where(PaperNativeTrade.deployment_id == card.id).order_by(PaperNativeTrade.closed_at.desc()).limit(1)
            )).first()
            if last is not None:
                closed = last.closed_at.astimezone(IST).strftime("%d-%b %H:%M:%S") if last.closed_at.tzinfo else f"{last.closed_at:%d-%b %H:%M:%S} UTC"
                print(f"    last closed trade: {last.exit_reason} at {closed}, {len(last.legs)} legs, exits "
                      + ", ".join(f"{leg['side']} {leg['exit_price']:,.2f}" for leg in last.legs))
    pools = (await db.execute(select(func.count()).select_from(PaperPortfolio).where(func.trim(PaperPortfolio.name) == NAME))).scalar()
    print(f"  pools named {NAME!r}: {pools}")



def _legs_at_book(deployment_id, owner_id) -> str:
    """What the running app's card shows for the position (GET .../position,
    as its owner): how many legs are valued at the book -- a sold leg at
    the ask, a bought one at the bid (11 Nov). Counts only."""
    import urllib.request

    request = urllib.request.Request(
        f"http://localhost:8000/api/v1/paper-trading/native-deployments/{deployment_id}/position",
        headers={"Authorization": f"Bearer {create_access_token(str(owner_id), [])}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            position = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001 -- the kind only, no detail
        return f"not read ({type(exc).__name__})"
    legs = (position or {}).get("legs") or []
    booked = [leg for leg in legs if leg.get("bid") is not None and leg.get("ask") is not None]
    right = [leg for leg in booked if leg["current_price"] == (leg["ask"] if leg["side"] == "short" else leg["bid"])]
    return f"{len(booked)} of {len(legs)} legs at bid/ask, {len(right)} at the right side; payoff shown: {bool((position or {}).get('payoff'))}"


async def stored_data(db) -> None:
    """What the server keeps for a backtest (17 Nov): the 15-minute change-in-OI
    records and the stored NIFTY option candles -- dates and counts only."""
    from app.models.backfill_platform import BfOhlcvBar, BfSymbol
    from app.models.pcr import PcrSnapshot, PcrStrikeOi

    first, last, records, sessions = (await db.execute(
        select(func.min(PcrSnapshot.session_date), func.max(PcrSnapshot.session_date), func.count(),
               func.count(func.distinct(PcrSnapshot.session_date))).where(PcrSnapshot.underlying == "NIFTY")
    )).one()
    with_15m = (await db.execute(select(func.count()).select_from(PcrSnapshot).where(
        PcrSnapshot.underlying == "NIFTY", PcrSnapshot.put_oi_change.is_not(None), PcrSnapshot.call_oi_change.is_not(None))
    )).scalar()
    strike_rows = (await db.execute(select(func.count()).select_from(PcrStrikeOi))).scalar()
    priced = (await db.execute(select(func.count()).select_from(PcrStrikeOi).where(PcrStrikeOi.last_price.is_not(None)))).scalar()
    print("stored data (17 Nov):")
    print(f"  NIFTY change-in-OI records: {records} across {sessions} sessions, {first} -> {last}; with a 15-min put/call change: {with_15m}")
    print(f"  per-contract rows on those records: {strike_rows} (with a last price: {priced})")
    rows = (await db.execute(
        select(BfOhlcvBar.timeframe, func.count(), func.count(func.distinct(BfOhlcvBar.symbol_id)), func.min(BfOhlcvBar.ts), func.max(BfOhlcvBar.ts))
        .join(BfSymbol, BfSymbol.id == BfOhlcvBar.symbol_id)
        .where(BfSymbol.underlying_symbol == "NIFTY", BfSymbol.option_type.is_not(None))
        .group_by(BfOhlcvBar.timeframe)
    )).all()
    for tf, n, contracts, lo, hi in rows:
        print(f"  NIFTY option candles {tf}: {n} bars, {contracts} contracts, {as_aware_utc(lo).astimezone(IST):%d-%b-%Y} -> "
              f"{as_aware_utc(hi).astimezone(IST):%d-%b-%Y %H:%M}")
    expiries = (await db.execute(
        select(BfSymbol.expiry, func.count()).where(BfSymbol.underlying_symbol == "NIFTY", BfSymbol.option_type.is_not(None))
        .group_by(BfSymbol.expiry).order_by(BfSymbol.expiry)
    )).all()
    print(f"  NIFTY option expiries with candles kept: {len(expiries)}: " + ", ".join(f"{e:%d %b}" for e, _ in expiries[-12:] if e))


async def main() -> None:
    today = datetime.now(timezone.utc).astimezone(IST).date()
    async with AsyncSessionLocal() as db:
        for NAME, FILE_MD5 in NAMES.items():
            await describe(db, NAME, FILE_MD5)

        await stored_data(db)

        nifty = (await db.execute(select(Instrument).where(Instrument.symbol == "NIFTY 50", Instrument.exchange == "NSE"))).scalars().first()
        rows = (await db.execute(
            select(Instrument.expiry, func.count(), func.min(Instrument.strike), func.max(Instrument.strike), func.max(Instrument.lot_size))
            .where(Instrument.underlying_instrument_id == nifty.id, Instrument.instrument_type == "option", Instrument.expiry >= today)
            .group_by(Instrument.expiry).order_by(Instrument.expiry)
        )).all()
        expiries = [r[0] for r in rows]
        monthly = {e for e in expiries if not any(o.year == e.year and o.month == e.month and o > e for o in expiries)}
        print(f"NIFTY option expiries from {today}: {len(rows)}")
        for e, n, lo, hi, lot in rows:
            print(f"  {e} {e:%a}: {n} contracts, strikes {lo:,.0f}-{hi:,.0f}, lot {lot}" + ("  <- last listed in its month" if e in monthly else ""))

        spot_row = (await db.execute(
            select(Instrument.strike).where(Instrument.underlying_instrument_id == nifty.id, Instrument.expiry == expiries[0])
        )).scalars().all() if expiries else []
        mid = sorted(spot_row)[len(spot_row) // 2] if spot_row else None
        sample: dict[str, str] = {}
        for e in expiries:
            for k in (mid - 50, mid, mid + 50) if mid else ():
                for t in ("CE", "PE"):
                    iid = (await db.execute(select(Instrument.id).where(
                        Instrument.underlying_instrument_id == nifty.id, Instrument.expiry == e, Instrument.strike == k, Instrument.option_type == t,
                    ))).scalars().first()
                    if iid:
                        sample[str(iid)] = str(e)
        user = (await db.execute(select(User).where(User.is_active.is_(True), User.is_approved.is_(True)).limit(1))).scalars().first()

    token = create_access_token(str(user.id), [])
    ticks: dict[str, dict] = {}
    async with websockets.connect(f"ws://localhost:8000/api/v1/ws/market-data?token={token}", max_size=2**22) as ws:
        await ws.send(json.dumps({"type": "subscribe", "instrument_ids": list(sample)}))
        deadline = time.monotonic() + LISTEN_SECONDS
        while (left := deadline - time.monotonic()) > 0:
            try:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
            except asyncio.TimeoutError:
                break
            if message.get("type") == "tick":
                ticks[message["instrument_id"]] = message
    for e in expiries:
        ids = [i for i, x in sample.items() if x == str(e)]
        real = [i for i in ids if ticks.get(i, {}).get("source") == "kite"]
        print(f"  streaming {e}: {len(real)} of {len(ids)} sampled contracts with a Kite price")


if __name__ == "__main__":
    asyncio.run(main())
