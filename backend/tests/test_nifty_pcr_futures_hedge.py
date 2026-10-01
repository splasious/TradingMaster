"""Nifty PCR Futures Hedge (native_strategies/nifty_pcr_futures_hedge.py):
PCR below 0.75 -> short future + short ITM PUT, out above 0.80; above 1.25
-> long future + short ITM CALL, out below 1.20; the option rolls at 09:20
on its expiry day, the future at 15:00 the trading day before its expiry.
A future books only its profit or loss (native_runner.open_leg/close_leg)."""

import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_pcr_futures_hedge as hedge
from app.models.instrument import Instrument
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext

SPOT = 23450.0
CASH = 1_000_000.0
QTY = 650.0  # 10 lots x 65
# Time value per weekly expiry: ITM premium = intrinsic + this.
TIME_VALUE = {date(2026, 10, 6): 60.0, date(2026, 10, 13): 150.0, date(2026, 10, 27): 300.0}


def at(day: int, hh: int, mm: int, ss: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hh, mm, ss, tzinfo=IST)


def premium(option: Instrument, spot: float) -> float:
    intrinsic = max(option.strike - spot, 0.0) if option.option_type == "PE" else max(spot - option.strike, 0.0)
    return intrinsic + TIME_VALUE[option.expiry]


class Market:
    """The deployment, its contracts, their live prices and the PCR."""

    def __init__(self, db, monkeypatch):
        self.db, self.monkeypatch, self.pcr = db, monkeypatch, None

    async def build(self) -> "Market":
        db = self.db
        user = User(email=f"hedge_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="Hedge")
        db.add(user)
        await db.flush()
        strategy = Strategy(name="Nifty PCR Futures Hedge", owner_id=user.id, code_type="native")
        db.add(strategy)
        await db.flush()
        version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                                  python_code="#", position_sizing={}, risk_rules={})
        self.portfolio = PaperPortfolio(user_id=user.id, name="Hedge", cash=CASH, initial_capital=CASH)
        db.add_all([version, self.portfolio])
        await db.flush()
        self.deployment = PaperNativeDeployment(portfolio_id=self.portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                                status=DeploymentStatus.ACTIVE.value, state=None)
        self.nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index", data_source="zerodha_kite",
                                external_ref="NIFTY 50")
        db.add_all([self.deployment, self.nifty])
        await db.flush()

        def contract(symbol, kind, expiry, strike=None, option_type=None):
            return Instrument(exchange="NFO", symbol=symbol, name=symbol, instrument_type=kind, data_source="zerodha_kite",
                              external_ref=symbol, expiry=expiry, strike=strike, option_type=option_type, lot_size=65,
                              underlying_instrument_id=self.nifty.id)

        self.oct_fut = contract("NIFTY26OCTFUT", "future", date(2026, 10, 27))
        self.nov_fut = contract("NIFTY26NOVFUT", "future", date(2026, 11, 23))
        self.options = [
            contract(f"NIFTY{expiry:%y%m%d}{strike}{kind}", "option", expiry, float(strike), kind)
            for expiry in TIME_VALUE for strike in range(23000, 24001, 50) for kind in ("PE", "CE")
        ]
        db.add_all([self.oct_fut, self.nov_fut, *self.options])
        await db.commit()

        async def get_pcr(ctx, *args, **kwargs):
            return self.pcr

        self.monkeypatch.setattr(NativeContext, "get_pcr", get_pcr)
        return self

    def option(self, expiry: date, strike: float, kind: str) -> Instrument:
        return next(o for o in self.options if o.expiry == expiry and o.strike == strike and o.option_type == kind)

    def prices(self, now: datetime, spot: float = SPOT, futures: float | None = None, skip: tuple = ()) -> None:
        """Live prices at `now`: NIFTY, both futures (spot + 30 unless given)
        and every option at intrinsic + its expiry's time value."""
        quotes = {self.nifty: spot, self.oct_fut: futures or spot + 30, self.nov_fut: (futures or spot + 30) + 120}
        quotes.update({o: premium(o, spot) for o in self.options})
        for instrument, price in quotes.items():
            if instrument in skip:
                tick_engine.forget([instrument.id])
                continue
            tick_engine.set_real_price(instrument.id, price, source="test")
            tick_engine._real_price_at[instrument.id] = now  # fresh as of the check, whatever the wall clock says

    async def check(self, now: datetime, pcr: float | None) -> NativeContext:
        self.pcr = pcr
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {}, now=now)
        await hedge.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    async def trades(self) -> list[PaperNativeTrade]:
        return (await self.db.execute(select(PaperNativeTrade).order_by(PaperNativeTrade.closed_at))).scalars().all()


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await Market(db_session, monkeypatch).build()
    yield m
    tick_engine.forget([m.nifty.id, m.oct_fut.id, m.nov_fut.id, *(o.id for o in m.options)])


def test_the_rules():
    assert hedge.entry_bias(0.74) == "bearish" and hedge.entry_bias(0.75) is None and hedge.entry_bias(1.26) == "bullish"
    assert hedge.exit_due("bearish", 0.81) and not hedge.exit_due("bearish", 0.80) and hedge.exit_due("bullish", 1.19)
    assert hedge.futures_roll_day(date(2026, 10, 27)) == date(2026, 10, 26)
    assert hedge.futures_roll_day(date(2026, 10, 6)) == date(2026, 10, 5)  # Monday, 2 Oct's holiday is before
    assert not hedge.option_roll_due(date(2026, 10, 6), at(6, 9, 19, 59)) and hedge.option_roll_due(date(2026, 10, 6), at(6, 9, 20))
    assert hedge.option_roll_due(date(2026, 10, 6), at(7, 9, 15))  # missed: rolled at the next check
    assert not hedge.futures_roll_due(date(2026, 10, 27), at(26, 14, 59)) and hedge.futures_roll_due(date(2026, 10, 27), at(26, 15, 0))
    # ITM closest to 150; the shallowest ITM when every one costs more.
    assert hedge.pick_strike([(23500, 110.0), (23550, 160.0), (23600, 210.0), (23400, 60.0)], "PE", SPOT) == (23550, 160.0)
    assert hedge.pick_strike([(23500, 200.0), (23550, 250.0)], "PE", SPOT) == (23500, 200.0)
    assert hedge.pick_strike([(23400, 110.0), (23350, 160.0), (23500, 150.0)], "CE", SPOT) == (23350, 160.0)


async def test_bearish_enters_below_075_and_both_legs_exit_above_080(market):
    market.prices(at(1, 10, 0, 5))
    ctx = await market.check(at(1, 10, 0, 5), 0.70)

    legs = ctx.state["position"]["legs"]
    assert ctx.state["position"]["bias"] == "bearish" and ctx._last_signal == "SHORT_FUT_PE"
    assert (legs["future"]["symbol"], legs["future"]["side"], legs["future"]["quantity"], legs["future"]["entry_price"]) == (
        "NIFTY26OCTFUT", "sell", QTY, SPOT + 30)
    # 6 Oct weekly, ITM put closest to 150: 23550 (100 in the money + 60 = 160).
    assert (legs["option"]["symbol"], legs["option"]["side"], legs["option"]["entry_price"]) == ("NIFTY26100623550PE", "sell", 160.0)
    assert market.portfolio.cash == CASH + QTY * 160.0  # the premium only -- the future moves no cash

    market.prices(at(1, 10, 15, 5))
    held = await market.check(at(1, 10, 15, 5), 0.78)  # between 0.75 and 0.80: held
    assert held.state["position"] is not None and held._last_action == "hold"

    # NIFTY falls 100: the short future gains 100/unit, the put costs 100 more.
    market.prices(at(1, 10, 30, 5), spot=SPOT - 100)
    out = await market.check(at(1, 10, 30, 5), 0.81)
    assert out.state["position"] is None and out._last_signal == "COVER"
    [trade] = await market.trades()
    assert trade.exit_reason == "pcr_above_0.80" and len(trade.legs) == 2
    assert trade.pnl == pytest.approx(QTY * 100 - QTY * 100)  # +65,000 future, -65,000 put
    assert market.portfolio.cash == pytest.approx(CASH + trade.pnl)


async def test_bullish_enters_above_125_and_exits_below_120(market):
    market.prices(at(1, 11, 0, 5))
    ctx = await market.check(at(1, 11, 0, 5), 1.30)
    legs = ctx.state["position"]["legs"]
    assert ctx._last_signal == "LONG_FUT_CE" and legs["future"]["side"] == "buy"
    assert (legs["option"]["symbol"], legs["option"]["entry_price"]) == ("NIFTY26100623350CE", 160.0)  # 100 ITM + 60

    market.prices(at(1, 11, 15, 5), spot=SPOT + 40)
    held = await market.check(at(1, 11, 15, 5), 1.21)
    assert held.state["position"] is not None

    market.prices(at(1, 11, 30, 5), spot=SPOT + 40)
    out = await market.check(at(1, 11, 30, 5), 1.19)
    [trade] = await market.trades()
    assert out.state["position"] is None and trade.exit_reason == "pcr_below_1.20"
    assert trade.pnl == pytest.approx(QTY * 40 - QTY * 40)  # long future +40/unit, short call -40/unit
    assert market.portfolio.cash == pytest.approx(CASH + trade.pnl)


async def test_no_entry_outside_the_window_in_the_neutral_zone_or_without_both_prices(market):
    market.prices(at(1, 9, 40))
    assert (await market.check(at(1, 9, 40), 0.70)).state.get("position") is None  # before 09:45
    market.prices(at(1, 10, 0))
    flat = await market.check(at(1, 10, 0), 1.00)
    assert flat.state.get("position") is None and "enters below 0.75 or above 1.25" in flat._last_reason
    assert (await market.check(at(1, 10, 1), None)).state.get("position") is None  # no PCR record

    market.prices(at(1, 10, 5), skip=(market.oct_fut,))
    missing = await market.check(at(1, 10, 5), 0.70)
    assert missing.state.get("position") is None and "no live price for NIFTY26OCTFUT" in missing._last_reason
    assert market.portfolio.cash == CASH  # neither leg opened


async def test_a_jump_from_bearish_to_bullish_exits_and_enters_on_the_same_check(market):
    market.prices(at(1, 10, 0))
    await market.check(at(1, 10, 0), 0.70)
    market.prices(at(1, 10, 15))
    ctx = await market.check(at(1, 10, 15), 1.30)
    assert ctx.state["position"]["bias"] == "bullish" and ctx._last_signal == "LONG_FUT_CE"
    assert "closed bearish" in ctx._last_reason
    assert [t.exit_reason for t in await market.trades()] == ["pcr_above_0.80"]


async def test_the_option_rolls_to_next_week_at_0920_on_its_expiry_day(market):
    market.prices(at(1, 10, 0))
    await market.check(at(1, 10, 0), 0.70)
    future_before = market.deployment.state["position"]["legs"]["future"]

    market.prices(at(6, 9, 19, 50))
    early = await market.check(at(6, 9, 19, 50), 0.78)
    assert early.state["position"]["legs"]["option"]["expiry"] == "2026-10-06" and not await market.trades()

    market.prices(at(6, 9, 20, 5))
    ctx = await market.check(at(6, 9, 20, 5), 0.78)
    legs = ctx.state["position"]["legs"]
    # 13 Oct weekly: even the shallowest ITM put costs 200 (50 + 150) -- taken.
    assert (legs["option"]["symbol"], legs["option"]["entry_price"]) == ("NIFTY26101323500PE", 200.0)
    assert legs["future"] == future_before and ctx._last_signal == "ROLL"
    [trade] = await market.trades()
    assert trade.exit_reason == "option_rollover" and len(trade.legs) == 1 and trade.legs[0]["exit_price"] == 160.0  # 6 Oct 23550 PE bought back

    # A new entry on an expiry day takes next week's option too.
    market.prices(at(6, 10, 0))
    await market.check(at(6, 10, 0), 0.81)  # exit
    market.prices(at(6, 10, 15))
    again = await market.check(at(6, 10, 15), 0.70)
    assert again.state["position"]["legs"]["option"]["expiry"] == "2026-10-13"


async def test_the_future_rolls_to_next_month_at_1500_the_day_before_expiry(market):
    market.prices(at(26, 10, 0))
    entered = await market.check(at(26, 10, 0), 1.30)
    # From the roll day on, an entry takes next month's future.
    assert entered.state["position"]["legs"]["future"]["symbol"] == "NIFTY26NOVFUT"
    await market.check(at(26, 10, 15), 1.19)  # out again
    market.deployment.state = None
    await market.db.commit()

    market.prices(at(23, 10, 0))
    await market.check(at(23, 10, 0), 1.30)  # Fri 23 Oct: October's future
    assert market.deployment.state["position"]["legs"]["future"]["symbol"] == "NIFTY26OCTFUT"
    market.deployment.state["position"]["legs"]["option"]["expiry"] = "2026-10-27"  # keep the option out of this test

    market.prices(at(26, 14, 59, 50), futures=SPOT + 80)
    early = await market.check(at(26, 14, 59, 50), 1.30)
    assert early.state["position"]["legs"]["future"]["symbol"] == "NIFTY26OCTFUT"

    trades_before = len(await market.trades())
    market.prices(at(26, 15, 0, 5), futures=SPOT + 80)
    ctx = await market.check(at(26, 15, 0, 5), 1.30)
    future = ctx.state["position"]["legs"]["future"]
    assert (future["symbol"], future["side"], future["entry_price"]) == ("NIFTY26NOVFUT", "buy", SPOT + 80 + 120)
    rolled = (await market.trades())[trades_before:]
    assert [t.exit_reason for t in rolled] == ["futures_rollover"] and rolled[0].pnl == pytest.approx(QTY * 50)  # 23480 -> 23530


async def test_stop_and_exit_closes_both_legs(market):
    market.prices(at(1, 10, 0))
    await market.check(at(1, 10, 0), 0.70)
    market.deployment.state = {**market.deployment.state, "force_exit": True}
    market.prices(at(1, 10, 1))
    ctx = await market.check(at(1, 10, 1), 0.70)  # still bearish: closed because it was asked
    assert ctx.state["position"] is None and [t.exit_reason for t in await market.trades()] == ["manual"]


async def test_a_future_books_only_its_profit_or_loss(market):
    ctx = NativeContext(db=market.db, portfolio=market.portfolio, deployment=market.deployment, state={}, now=at(1, 10, 0))
    await ctx.open_leg(market.oct_fut, "sell", QTY, 23480.0)
    assert market.portfolio.cash == CASH
    with pytest.raises(ValueError):
        await ctx.close_leg(market.oct_fut, "buy", QTY, 23400.0)
    await ctx.close_leg(market.oct_fut, "buy", QTY, 23400.0, entry_price=23480.0)
    assert market.portfolio.cash == pytest.approx(CASH + QTY * 80)
    option = market.option(date(2026, 10, 6), 23550.0, "PE")  # an option still moves its premium
    await ctx.open_leg(option, "sell", QTY, 160.0)
    await ctx.close_leg(option, "buy", QTY, 100.0)
    assert market.portfolio.cash == pytest.approx(CASH + QTY * 80 + QTY * 60)


async def test_the_card_shows_the_future_as_profit_or_loss(market):
    from app.api.v1.endpoints.paper_native_trading import _build_position_out

    market.prices(at(1, 10, 0))
    await market.check(at(1, 10, 0), 0.70)
    market.prices(at(1, 10, 5), spot=SPOT - 100)  # future +100/unit (short), put -100/unit
    out = await _build_position_out(market.db, market.deployment.state)
    assert out.trade_value == pytest.approx(QTY * 160.0) and out.live_value == pytest.approx(QTY * 260.0)  # the put only
    assert out.unrealized_pnl == pytest.approx(QTY * 100 - QTY * 100)


async def test_a_backtest_books_a_future_the_same_way(market):
    from app.services.backtest.native_runner import BacktestNativeContext

    ctx = BacktestNativeContext(db=market.db, portfolio=market.portfolio, deployment=market.deployment, state={}, now=at(1, 10, 0))
    await ctx.open_leg(market.oct_fut, "buy", QTY, 23480.0)
    assert market.portfolio.cash == CASH
    await ctx.close_leg(market.oct_fut, "sell", QTY, 23500.0, entry_price=23480.0)
    assert market.portfolio.cash == pytest.approx(CASH + QTY * 20)
