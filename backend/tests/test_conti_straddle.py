"""Conti Straddle 15 MIN (native_strategies/nifty_conti_straddle.py, 21 Oct):
a NIFTY short straddle held from 09:45 to 15:00 whatever the PCR; the PCR
picks the strike -- below 1 the CALL premium just above the PUT, 1 or above
the PUT just above the CALL -- kept until a 100-point roll reads it again."""

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_conti_straddle as conti
from app.models.instrument import Instrument
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext

EXPIRY = date(2026, 10, 27)


def at(hh: int, mm: int, ss: int = 0, day: int = 21) -> datetime:
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=IST)


def test_the_pcr_picks_the_call_or_put_heavy_strike_nearest_parity():
    # CE - PE = NIFTY - strike: below NIFTY the CALL leads, above it the PUT does.
    spot = 23460.0
    quotes = {k: (100 + (spot - k) / 2, 100 + (k - spot) / 2) for k in conti.candidate_strikes(spot)}
    assert conti.candidate_strikes(spot) == [23300, 23350, 23400, 23450, 23500, 23550, 23600]
    assert (conti.side_for(0.99), conti.side_for(1.0)) == ("call", "put")
    assert conti.pick_strike(quotes, "call", spot) == 23450  # CE ahead by 10
    assert conti.pick_strike(quotes, "put", spot) == 23500  # PE ahead by 40
    assert conti.pick_strike({23450: (90.0, 100.0)}, "call", spot) is None  # no strike where the CALL leads


class Market:
    def __init__(self, db):
        self.db, self.pcr, self.missing = db, None, set()

    async def build(self, monkeypatch) -> "Market":
        db = self.db
        user = User(email=f"conti_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="Conti")
        db.add(user)
        await db.flush()
        strategy = Strategy(name="Conti Straddle 15 MIN", owner_id=user.id, code_type="native")
        db.add(strategy)
        await db.flush()
        version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                                  python_code="#", position_sizing={}, risk_rules={})
        self.portfolio = PaperPortfolio(user_id=user.id, name="Conti Straddle 15 MIN", cash=2_000_000.0, initial_capital=2_000_000.0)
        db.add_all([version, self.portfolio])
        await db.flush()
        self.deployment = PaperNativeDeployment(portfolio_id=self.portfolio.id, strategy_id=strategy.id,
                                                strategy_version_id=version.id, status=DeploymentStatus.ACTIVE.value, state=None)
        self.nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index",
                                data_source="zerodha_kite", external_ref="NIFTY 50")
        db.add_all([self.deployment, self.nifty])
        await db.flush()
        self.options = [
            Instrument(exchange="NFO", symbol=f"NIFTY26O27{k}{t}", name="opt", instrument_type="option", data_source="zerodha_kite",
                       external_ref=f"NIFTY26O27{k}{t}", expiry=EXPIRY, strike=float(k), option_type=t, lot_size=65,
                       underlying_instrument_id=self.nifty.id)
            for k in range(23000, 24001, 50) for t in ("CE", "PE")
        ]
        db.add_all(self.options)
        await db.commit()

        async def get_pcr(ctx, *args, **kwargs):
            return self.pcr

        async def expiries(ctx, *args, **kwargs):
            return [EXPIRY]

        monkeypatch.setattr(NativeContext, "get_pcr", get_pcr)
        monkeypatch.setattr(NativeContext, "list_weekly_expiries", expiries)
        return self

    def prices(self, now: datetime, spot: float) -> None:
        tick_engine.set_real_price(self.nifty.id, spot, source="test")
        tick_engine._real_price_at[self.nifty.id] = now
        for o in self.options:
            if (o.strike, o.option_type) in self.missing:
                tick_engine.forget([o.id])
                continue
            lead = (spot - o.strike) / 2  # CE - PE = NIFTY - strike
            tick_engine.set_real_price(o.id, 100 + lead if o.option_type == "CE" else 100 - lead, source="test")
            tick_engine._real_price_at[o.id] = now

    async def check(self, now: datetime, spot: float, pcr: float | None) -> NativeContext:
        self.pcr = pcr
        self.prices(now, spot)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await conti.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    async def trades(self) -> list[PaperNativeTrade]:
        return (await self.db.execute(select(PaperNativeTrade).order_by(PaperNativeTrade.closed_at))).scalars().all()


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await Market(db_session).build(monkeypatch)
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def legs(ctx) -> tuple[float, str, float]:
    p = ctx.state["position"]
    return p["strike"], p["side"], p["legs"]["short_ce"]["quantity"]


async def test_it_waits_for_0945_then_holds_a_straddle_whatever_the_pcr(market):
    early = await market.check(at(9, 40), 23460.0, 0.9)
    assert early.state.get("position") is None and "outside entry window" in early._last_reason
    opened = await market.check(at(9, 45, 5), 23460.0, 1.62)  # far outside AM OP's band: still a straddle
    assert legs(opened) == (23500, "put", 650.0) and opened._last_signal == "STRADDLE"


async def test_the_side_chosen_at_entry_is_kept_until_a_roll_reads_the_pcr_again(market):
    first = await market.check(at(9, 45, 5), 23460.0, 0.92)
    assert legs(first) == (23450, "call", 650.0)
    crossed = await market.check(at(10, 30), 23500.0, 1.08)  # PCR over 1, NIFTY 40 points on: held
    assert legs(crossed) == (23450, "call", 650.0) and await market.trades() == []

    rolled = await market.check(at(11, 0), 23561.0, 1.08)  # 101 points: closes and reopens, put-heavy now
    [trade] = await market.trades()
    assert trade.exit_reason == "rollover" and [leg["side"] for leg in trade.legs] == ["short", "short"]
    assert legs(rolled) == (23600, "put", 650.0) and "rolled to 23,600" in rolled._last_reason


async def test_a_roll_without_a_pcr_record_keeps_the_previous_side(market):
    await market.check(at(9, 45, 5), 23460.0, 0.92)
    rolled = await market.check(at(11, 0), 23359.0, None)  # 101 points down
    assert legs(rolled) == (23350, "call", 650.0)


async def test_everything_closes_at_1500_and_nothing_opens_after(market):
    await market.check(at(9, 45, 5), 23460.0, 0.92)
    closed = await market.check(at(15, 0, 3), 23470.0, 0.92)
    [trade] = await market.trades()
    assert closed.state["position"] is None and trade.exit_reason == "time_cutoff_3pm"
    after = await market.check(at(15, 0, 13), 23470.0, 0.92)
    assert after.state["position"] is None and "outside entry window" in after._last_reason


async def test_no_strike_with_live_premiums_on_the_right_side_skips_that_check(market):
    market.missing = {(k, t) for k in (23300.0, 23350.0, 23400.0, 23450.0) for t in ("CE", "PE")}
    skipped = await market.check(at(9, 45, 5), 23460.0, 0.92)  # only strikes above NIFTY quoted: no CALL leads
    assert skipped.state.get("position") is None and "CALL premium above the other" in skipped._last_reason
    market.missing = set()
    assert legs(await market.check(at(9, 45, 15), 23460.0, 0.92)) == (23450, "call", 650.0)
