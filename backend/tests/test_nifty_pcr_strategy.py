"""NIFTY PCR Strategy (native_strategies/nifty_pcr_strategy.py), the user's
TOTAL_OI_PCR state machine: PCR from the 15-minute record's nearest-weekly
strikes (ATM +/- 20), decisions at each completed close 09:45-15:15,
BULLISH / BEARISH / NEUTRAL with the file's exits, the neutral 100-point
recenter and 15:10 close + lock, and the rolls (the agreed 15:00 one too)."""

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_pcr_strategy as strat
from app.models.instrument import Instrument
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.pcr import SOURCE_LIVE, PcrSnapshot, PcrStrikeOi
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext

SPOT = 23450.0
CASH = 1_000_000.0
QTY = 650.0  # 10 lots x 65
STARTED = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)  # the deployment's start, before every test's session
# Time value per weekly expiry: premium = intrinsic + this.
TIME_VALUE = {date(2026, 10, 6): 60.0, date(2026, 10, 13): 150.0, date(2026, 10, 27): 100.0, date(2026, 11, 3): 150.0}


def at(day: int, hh: int, mm: int, ss: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hh, mm, ss, tzinfo=IST)


def premium(option: Instrument, spot: float) -> float:
    intrinsic = max(option.strike - spot, 0.0) if option.option_type == "PE" else max(spot - option.strike, 0.0)
    return intrinsic + TIME_VALUE[option.expiry]


class Market:
    def __init__(self, db):
        self.db = db

    async def build(self) -> "Market":
        db = self.db
        user = User(email=f"pcrs_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="PCR")
        db.add(user)
        await db.flush()
        strategy = Strategy(name="NIFTY PCR Strategy", owner_id=user.id, code_type="native")
        db.add(strategy)
        await db.flush()
        version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                                  python_code="#", position_sizing={}, risk_rules={})
        self.portfolio = PaperPortfolio(user_id=user.id, name="PCR", cash=CASH, initial_capital=CASH)
        db.add_all([version, self.portfolio])
        await db.flush()
        self.deployment = PaperNativeDeployment(portfolio_id=self.portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                                status=DeploymentStatus.ACTIVE.value, state=None, created_at=STARTED)
        self.nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index", data_source="zerodha_kite",
                                external_ref="NIFTY 50")
        db.add_all([self.deployment, self.nifty])
        await db.flush()

        def contract(symbol, kind, expiry, strike=None, option_type=None):
            return Instrument(exchange="NFO", symbol=symbol, name=symbol, instrument_type=kind, data_source="zerodha_kite",
                              external_ref=symbol, expiry=expiry, strike=strike, option_type=option_type, lot_size=65,
                              underlying_instrument_id=self.nifty.id)

        self.oct_fut = contract("NIFTY26OCTFUT", "future", date(2026, 10, 27))
        self.nov_fut = contract("NIFTY26NOVFUT", "future", date(2026, 11, 24))
        self.options = [
            contract(f"NIFTY{expiry:%y%m%d}{strike}{kind}", "option", expiry, float(strike), kind)
            for expiry in TIME_VALUE for strike in range(22400, 24551, 50) for kind in ("PE", "CE")
        ]
        db.add_all([self.oct_fut, self.nov_fut, *self.options])
        await db.commit()
        return self

    def option(self, expiry: date, strike: float, kind: str) -> Instrument:
        return next(o for o in self.options if o.expiry == expiry and o.strike == strike and o.option_type == kind)

    def prices(self, now: datetime, spot: float = SPOT) -> None:
        quotes = {self.nifty: spot, self.oct_fut: spot + 30, self.nov_fut: spot + 150}
        quotes.update({o: premium(o, spot) for o in self.options})
        for instrument, price in quotes.items():
            tick_engine.set_real_price(instrument.id, price, source="test")
            tick_engine._real_price_at[instrument.id] = now

    async def record(self, mark: datetime, pcr: float, spot: float = SPOT, expiries=(date(2026, 10, 6), date(2026, 10, 13)),
                     missing: int = 0) -> None:
        """A 15-minute PCR record at `mark`: nearest expiry's ATM +/- 20 at
        `pcr`; noise outside the window and on later expiries that must not
        count; `missing` strikes without a put OI."""
        snap = PcrSnapshot(underlying="NIFTY", ts=mark.astimezone(timezone.utc), session_date=mark.date(), captured_at=mark,
                           source=SOURCE_LIVE, strike_window=40, expiries=[e.isoformat() for e in expiries], spot=spot)
        self.db.add(snap)
        await self.db.flush()
        atm = strat.atm_strike(spot)
        rows = []
        for i in range(-30, 31):
            strike = atm + i * 50
            inside = abs(i) <= 20
            call = 1000.0
            put = call * pcr if inside else call * 9  # outside the window: must be ignored
            rows += [(expiries[0], strike, "CE", call), (expiries[0], strike, "PE", None if inside and i < -20 + missing else put)]
            rows += [(expiries[1], strike, "CE", 10.0), (expiries[1], strike, "PE", 900.0)]  # later expiry: ignored
        self.db.add_all([PcrStrikeOi(snapshot_id=snap.id, expiry=e, strike=k, option_type=t, tradingsymbol=f"N{k}{t}", oi=oi)
                         for e, k, t, oi in rows])
        await self.db.commit()

    async def check(self, now: datetime, spot: float = SPOT) -> NativeContext:
        self.prices(now, spot)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {}, now=now)
        await strat.evaluate(ctx)
        self.deployment.state = dict(ctx.state)
        await self.db.commit()
        return ctx

    async def trades(self) -> list[PaperNativeTrade]:
        return (await self.db.execute(select(PaperNativeTrade).order_by(PaperNativeTrade.closed_at))).scalars().all()


@pytest.fixture
async def market(db_session):
    m = await Market(db_session).build()
    yield m
    tick_engine.forget([m.nifty.id, m.oct_fut.id, m.nov_fut.id, *(o.id for o in m.options)])


def test_the_rules():
    assert strat.atm_strike(23474.9) == 23450 and strat.atm_strike(23475) == 23500
    assert [strat.entry_regime(p) for p in (1.2501, 1.25, 1.21, 1.20, 0.80, 0.79, 0.75, 0.7499)] == [
        "BULLISH", None, None, "NEUTRAL", "NEUTRAL", None, None, "BEARISH"]
    assert strat.exit_due("BULLISH", 1.19) and not strat.exit_due("BULLISH", 1.20)
    assert strat.exit_due("BEARISH", 0.81) and not strat.exit_due("BEARISH", 0.80)
    assert strat.exit_due("NEUTRAL", 0.74) and strat.exit_due("NEUTRAL", 1.26) and not strat.exit_due("NEUTRAL", 1.25)
    # completed closes 09:45-15:15 only
    assert strat.signal_mark(at(5, 9, 44, 59)) is None  # the 09:30 close isn't acted on
    assert strat.signal_mark(at(5, 9, 45, 3)) == at(5, 9, 45).astimezone(timezone.utc)
    assert strat.signal_mark(at(5, 15, 20)) == at(5, 15, 15).astimezone(timezone.utc)
    assert strat.signal_mark(at(5, 15, 30, 5)) is None
    # Rs 150 closest, then nearer spot; any strike
    assert strat.pick_near_premium([(23350, 160.0), (23400, 110.0), (23500, 140.0)], SPOT) == (23500, 140.0)
    assert strat.pick_near_premium([(23300, 140.0), (23600, 160.0)], SPOT) == (23300, 140.0)
    # straddle: nearest spot, then bigger combined premium
    assert strat.pick_straddle([(23400, 100.0, 80.0), (23450, 90.0, 90.0), (23500, 70.0, 110.0)], 23460) == (23450, 90.0, 90.0)
    assert strat.pick_straddle([(23400, 100.0, 80.0), (23450, None, 90.0)], 23440) == (23400, 100.0, 80.0)


def test_total_oi_pcr_counts_the_window_and_both_sided_strikes_only():
    rows = []
    for i in range(-25, 26):
        k = 23450 + i * 50
        rows += [(k, "CE", 100.0), (k, "PE", 100.0 if abs(i) <= 20 else 5000.0)]
    assert strat.total_oi_pcr(rows, SPOT) == (1.0, "41 strikes")
    rows.append((23450, "CE", None))  # the ATM call loses its OI: strike left out
    pcr, detail = strat.total_oi_pcr([r for r in rows if r != (23450, "CE", 100.0)], SPOT)
    assert pcr == 1.0 and detail == "40 strikes"
    sparse = [(k, t, None if t == "PE" and k < 23450 else oi) for k, t, oi in rows]
    assert strat.total_oi_pcr(sparse, SPOT)[0] is None  # under 90% of the strikes priced


async def test_nothing_before_the_0945_close_and_nothing_until_its_record_arrives(market):
    await market.record(at(5, 9, 30), 1.40)
    ctx = await market.check(at(5, 9, 40))
    assert ctx.state["regime"] == "FLAT" and ctx._last_signal is None
    ctx = await market.check(at(5, 9, 45, 2))  # 09:45 record not stored yet
    assert ctx.state["regime"] == "FLAT" and "waiting for the 09:45 PCR record" in ctx._last_reason


async def test_a_run_started_mid_session_waits_for_the_next_close(market):
    market.deployment.created_at = at(5, 10, 52)  # switched on at 10:52 -- the 10:45 close came before it
    await market.record(at(5, 10, 45), 0.70)
    ctx = await market.check(at(5, 10, 52, 30))
    assert ctx.state["regime"] == "FLAT" and ctx._last_signal is None and "first signal at the next close" in ctx._last_reason
    await market.record(at(5, 11, 0), 0.70)
    ctx = await market.check(at(5, 11, 0, 6))
    assert ctx.state["regime"] == "BEARISH" and ctx._last_signal == "ENTER_BEAR"


async def test_bullish_entry_exit_and_the_next_close_decides_again(market):
    await market.record(at(5, 9, 45), 1.30)
    ctx = await market.check(at(5, 9, 45, 8))
    legs = ctx.state["legs"]
    assert ctx.state["regime"] == "BULLISH" and ctx._last_signal == "ENTER_BULL"
    assert (legs["future"]["symbol"], legs["future"]["side"], legs["future"]["quantity"], legs["future"]["entry_price"]) == (
        "NIFTY26OCTFUT", "buy", QTY, SPOT + 30)
    # 6 Oct CE closest to Rs 150: 23350 (100 in the money + 60 = 160)
    assert (legs["option"]["symbol"], legs["option"]["side"], legs["option"]["entry_price"]) == ("NIFTY26100623350CE", "sell", 160.0)
    assert market.portfolio.cash == CASH + QTY * 160.0  # the future moves no cash

    again = await market.check(at(5, 9, 50))
    assert again.state["legs"] == legs  # the 09:45 close is acted on once

    await market.record(at(5, 10, 0), 1.00)  # exit (below 1.20) -- not a neutral entry at the same close
    ctx = await market.check(at(5, 10, 0, 6), spot=SPOT + 100)
    assert ctx.state["regime"] == "FLAT" and ctx._last_signal == "EXIT"
    [trade] = await market.trades()
    # future +100 x 650; CE 23350 now 200 in the money + 60 = 260: -100 x 650
    assert trade.pnl == pytest.approx(QTY * 100 - QTY * 100) and len(trade.legs) == 2
    await market.record(at(5, 10, 15), 1.00)
    ctx = await market.check(at(5, 10, 15, 5), spot=SPOT + 100)
    assert ctx.state["regime"] == "NEUTRAL" and ctx._last_signal == "ENTER_NEUTRAL"


async def test_bearish_short_future_and_put_near_150(market):
    await market.record(at(5, 9, 45), 0.70)
    ctx = await market.check(at(5, 9, 45, 8))
    legs = ctx.state["legs"]
    assert ctx.state["regime"] == "BEARISH" and legs["future"]["side"] == "sell"
    assert (legs["option"]["symbol"], legs["option"]["entry_price"]) == ("NIFTY26100623550PE", 160.0)
    await market.record(at(5, 10, 0), 0.78)  # still <= 0.80: held
    assert (await market.check(at(5, 10, 0, 5))).state["regime"] == "BEARISH"
    await market.record(at(5, 10, 15), 0.81)
    assert (await market.check(at(5, 10, 15, 5))).state["regime"] == "FLAT"


async def test_neutral_straddle_recenters_on_100_points_and_closes_at_1510(market):
    await market.record(at(5, 9, 45), 1.00)
    ctx = await market.check(at(5, 9, 45, 8))
    legs = ctx.state["legs"]
    assert ctx.state["regime"] == "NEUTRAL" and ctx.state["straddle_strike"] == 23450 and ctx.state["reference_spot"] == SPOT
    assert {legs["ce"]["symbol"], legs["pe"]["symbol"]} == {"NIFTY26100623450CE", "NIFTY26100623450PE"}

    assert (await market.check(at(5, 10, 3), spot=SPOT + 99)).state["straddle_strike"] == 23450  # not yet
    ctx = await market.check(at(5, 10, 4), spot=SPOT + 110)  # between closes: the recenter doesn't wait for one
    assert ctx._last_signal == "STRADDLE_SHIFT" and ctx.state["straddle_strike"] == 23550
    assert ctx.state["reference_spot"] == SPOT + 110
    assert (await market.trades())[-1].exit_reason == "neutral_recenter"

    ctx = await market.check(at(5, 15, 10, 1), spot=SPOT + 110)
    assert ctx.state["regime"] == "FLAT" and ctx.state["neutral_locked_date"] == "2026-10-05"
    assert (await market.trades())[-1].exit_reason == "neutral_1510_close"
    await market.record(at(5, 15, 15), 1.00, spot=SPOT + 110)
    ctx = await market.check(at(5, 15, 15, 5), spot=SPOT + 110)
    assert ctx.state["regime"] == "FLAT" and "15:10 lock" in ctx._last_reason


async def test_a_bullish_entry_is_still_allowed_after_1510(market):
    await market.record(at(5, 15, 15), 1.30)
    ctx = await market.check(at(5, 15, 15, 5))
    assert ctx.state["regime"] == "BULLISH"  # held overnight
    ctx = await market.check(at(5, 15, 25))
    assert ctx.state["regime"] == "BULLISH"


async def test_no_neutral_entry_at_the_1515_close(market):
    await market.record(at(5, 15, 15), 1.00)
    ctx = await market.check(at(5, 15, 15, 5))
    assert ctx.state["regime"] == "FLAT" and "neutral entries end at 15:10" in ctx._last_reason


async def test_neutral_exits_outside_075_125(market):
    await market.record(at(5, 9, 45), 1.10)
    await market.check(at(5, 9, 45, 8))
    await market.record(at(5, 10, 0), 1.24)
    assert (await market.check(at(5, 10, 0, 5))).state["regime"] == "NEUTRAL"
    await market.record(at(5, 10, 15), 1.26)
    assert (await market.check(at(5, 10, 15, 5))).state["regime"] == "FLAT"


async def test_on_the_option_expiry_day_it_rolls_when_pcr_is_past_entry(market):
    await market.record(at(5, 9, 45), 1.30)
    await market.check(at(5, 9, 45, 8))
    exp = (date(2026, 10, 6), date(2026, 10, 13))
    await market.record(at(6, 9, 45), 1.30, expiries=exp)
    ctx = await market.check(at(6, 9, 45, 8))
    # next week's CE closest to 150 (time value 150): the ATM 23450, nothing in the money
    assert ctx.state["legs"]["option"]["symbol"] == "NIFTY26101323450CE" and ctx._last_signal == "ROLL"
    assert (await market.trades())[-1].exit_reason == "option_rollover"


async def test_otherwise_the_option_rolls_at_the_1500_close(market):
    await market.record(at(5, 9, 45), 1.30)
    await market.check(at(5, 9, 45, 8))
    exp = (date(2026, 10, 6), date(2026, 10, 13))
    await market.record(at(6, 9, 45), 1.22, expiries=exp)  # held, but not past 1.25: no roll
    assert (await market.check(at(6, 9, 45, 8))).state["legs"]["option"]["expiry"] == "2026-10-06"
    await market.record(at(6, 14, 45), 1.22, expiries=exp)
    assert (await market.check(at(6, 14, 45, 8))).state["legs"]["option"]["expiry"] == "2026-10-06"
    await market.record(at(6, 15, 0), 1.22, expiries=exp)
    ctx = await market.check(at(6, 15, 0, 6))
    assert ctx.state["legs"]["option"]["expiry"] == "2026-10-13"


async def test_a_late_1500_record_rolls_at_1505(market):
    await market.record(at(5, 9, 45), 1.30)
    await market.check(at(5, 9, 45, 8))
    assert (await market.check(at(6, 15, 4))).state["legs"]["option"]["expiry"] == "2026-10-06"
    assert (await market.check(at(6, 15, 5, 1))).state["legs"]["option"]["expiry"] == "2026-10-13"


async def test_an_entry_on_the_expiry_day_takes_next_weeks_option(market):
    exp = (date(2026, 10, 6), date(2026, 10, 13))
    await market.record(at(6, 9, 45), 0.70, expiries=exp)
    ctx = await market.check(at(6, 9, 45, 8))
    assert ctx.state["legs"]["option"]["expiry"] == "2026-10-13"
    assert await market.trades() == []  # straight to next week's: not sold and rolled the same moment


async def test_the_future_rolls_on_its_expiry_day(market):
    exp = (date(2026, 10, 27), date(2026, 11, 3))
    await market.record(at(26, 9, 45), 1.30, expiries=exp)
    ctx = await market.check(at(26, 9, 45, 8))
    assert ctx.state["legs"]["future"]["symbol"] == "NIFTY26OCTFUT" and ctx.state["legs"]["option"]["expiry"] == "2026-10-27"
    await market.record(at(27, 9, 45), 1.22, expiries=exp)  # valid for the future (>= 1.20), not past entry for the option
    ctx = await market.check(at(27, 9, 45, 8))
    assert ctx.state["legs"]["future"]["symbol"] == "NIFTY26NOVFUT" and ctx.state["legs"]["future"]["side"] == "buy"
    assert ctx.state["legs"]["option"]["expiry"] == "2026-10-27"
    assert [t.exit_reason for t in await market.trades()] == ["futures_rollover"]


async def test_an_option_expired_while_stopped_settles_and_rolls(market):
    await market.record(at(5, 9, 45), 1.30)
    await market.check(at(5, 9, 45, 8))
    ctx = await market.check(at(7, 10, 0), spot=SPOT + 50)  # 6 Oct passed unseen
    assert ctx.state["legs"]["option"]["expiry"] == "2026-10-13"
    [trade] = await market.trades()
    assert trade.legs[0]["exit_price"] == 150.0  # CE 23350 at expiry: intrinsic only


async def test_an_untrustworthy_record_skips_that_close(market):
    await market.record(at(5, 9, 45), 1.30, missing=10)
    ctx = await market.check(at(5, 9, 45, 8))
    assert ctx.state["regime"] == "FLAT" and "of 41 strikes have both OIs" in ctx._last_reason
    assert ctx.state["last_signal_mark"] == at(5, 9, 45).astimezone(timezone.utc).isoformat()


async def test_exit_positions_closes_everything(market):
    await market.record(at(5, 9, 45), 1.00)
    await market.check(at(5, 9, 45, 8))
    market.deployment.state = {**market.deployment.state, "force_exit": True}
    await market.db.commit()
    ctx = await market.check(at(5, 11, 0))
    assert ctx.state["regime"] == "FLAT" and ctx._last_signal == "EXIT"
    assert (await market.trades())[-1].exit_reason == "manual"


def test_it_is_offered_as_a_built_in():
    from app.api.v1.endpoints.strategies import _native_builtin

    builtin = _native_builtin("nifty_pcr_strategy")
    assert builtin["title"].startswith("NIFTY PCR Strategy") and builtin["version"] == 2
