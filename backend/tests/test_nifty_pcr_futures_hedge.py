"""Nifty PCR Futures Hedge (native_strategies/nifty_pcr_futures_hedge.py),
intraday since 15 Oct: PCR below 0.90 with NIFTY 50 under its MA -> short
future + short ITM PUT, out above 1.05; above 1.10 with NIFTY over the MA ->
long future + short ITM CALL, out below 0.95. The MA -- 8 completed 15-minute
NIFTY 50 candles' (O+H+L+C)/4 -- trails: NIFTY crossing it closes the trade,
and that side re-enters after a 15-minute close back across it. Entries
09:30-15:00, everything closed at 15:00. A future books only its profit or
loss (native_runner.open_leg/close_leg)."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_pcr_futures_hedge as hedge
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
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
        self.monkeypatch.setattr(hedge, "note_native_candle_demand", lambda *a, **k: None, raising=False)

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
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))  # as the runner passes it
        await hedge.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    async def candles(self, first: datetime, last: datetime, level: float) -> None:
        """NIFTY 50 15-minute candles opening at `first` .. `last` (same
        day), each flat at `level` -- so (O+H+L+C)/4 = `level`."""
        ts = first
        while ts <= last:
            self.db.add(OhlcvCandle(instrument_id=self.nifty.id, timeframe="15m", ts=ts.astimezone(timezone.utc),
                                    open=level, high=level, low=level, close=level, volume=None, source="test"))
            ts += timedelta(minutes=15)
        await self.db.commit()

    async def history(self, level: float, until: datetime) -> None:
        """The MA at `level`: 30 Sep 13:00-15:15, then 1 Oct's candles opening before `until`."""
        await self.candles(at(30, 13, 0, month=9), at(30, 15, 15, month=9), level)
        if until > at(1, 9, 15):
            await self.candles(at(1, 9, 15), until - timedelta(minutes=15), level)

    async def trades(self) -> list[PaperNativeTrade]:
        return (await self.db.execute(select(PaperNativeTrade).order_by(PaperNativeTrade.closed_at))).scalars().all()


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await Market(db_session, monkeypatch).build()
    yield m
    tick_engine.forget([m.nifty.id, m.oct_fut.id, m.nov_fut.id, *(o.id for o in m.options)])


def test_the_rules():
    assert hedge.entry_bias(0.89) == "bearish" and hedge.entry_bias(0.90) is None and hedge.entry_bias(1.10) is None
    assert hedge.entry_bias(1.11) == "bullish"
    assert hedge.exit_due("bearish", 1.06) and not hedge.exit_due("bearish", 1.05)
    assert hedge.exit_due("bullish", 0.94) and not hedge.exit_due("bullish", 0.95)
    assert hedge.futures_roll_day(date(2026, 10, 27)) == date(2026, 10, 26)
    assert hedge.futures_roll_day(date(2026, 10, 6)) == date(2026, 10, 5)  # Monday, 2 Oct's holiday is before
    # ITM closest to 150; the shallowest ITM when every one costs more.
    assert hedge.pick_strike([(23500, 110.0), (23550, 160.0), (23600, 210.0), (23400, 60.0)], "PE", SPOT) == (23550, 160.0)
    assert hedge.pick_strike([(23500, 200.0), (23550, 250.0)], "PE", SPOT) == (23500, 200.0)
    assert hedge.pick_strike([(23400, 110.0), (23350, 160.0), (23500, 150.0)], "CE", SPOT) == (23350, 160.0)


def test_the_ma_is_eight_candles_of_ohlc_over_4():
    def bar(o, h, lo, c):
        return {"open": o, "high": h, "low": lo, "close": c}

    assert hedge.typical(bar(100, 120, 90, 110)) == 105
    bars = [bar(100, 100, 100, 100)] * 2 + [bar(100, 120, 90, 110)] * 7 + [bar(200, 200, 200, 200)]
    assert hedge.moving_average(bars) == pytest.approx((105 * 7 + 200) / 8)  # the last 8 only
    assert hedge.moving_average(bars[:7]) is None
    candle = {"ts": at(1, 10, 0).astimezone(timezone.utc), "close": 99.9}  # ends 10:15
    assert hedge.ma_stop_due("bullish", candle, 100, at(1, 10, 5)) and not hedge.ma_stop_due("bearish", candle, 100, at(1, 10, 5))
    assert not hedge.ma_stop_due("bullish", candle, 100, at(1, 10, 15))  # closed by the entry: doesn't count
    assert hedge.ma_stop_due("bearish", {**candle, "close": 100.1}, 100, at(1, 10, 5))


def test_the_ma_counts_only_with_recent_candles():
    assert hedge.ma_is_current(at(1, 10, 0), at(1, 10, 30)) and not hedge.ma_is_current(at(1, 10, 0), at(1, 10, 31))
    # At the open the previous session's last candle counts, for the first half hour.
    assert hedge.ma_is_current(at(30, 15, 30, month=9), at(1, 9, 30)) and not hedge.ma_is_current(at(30, 15, 30, month=9), at(1, 9, 46))
    assert hedge.ma_is_current(at(1, 15, 30), at(5, 9, 20))  # Monday after Thursday: 2 Oct and the weekend shut
    assert not hedge.ma_is_current(at(29, 15, 30, month=9), at(1, 9, 30))  # two sessions back: behind


async def test_bearish_enters_below_090_under_the_ma_and_both_legs_exit_above_105(market):
    await market.history(23500.0, at(1, 10, 0))  # MA 23500, NIFTY 23450 under it
    market.prices(at(1, 10, 0, 5))
    ctx = await market.check(at(1, 10, 0, 5), 0.85)

    legs = ctx.state["position"]["legs"]
    assert ctx.state["position"]["bias"] == "bearish" and ctx._last_signal == "SHORT_FUT_PE"
    assert (legs["future"]["symbol"], legs["future"]["side"], legs["future"]["quantity"], legs["future"]["entry_price"]) == (
        "NIFTY26OCTFUT", "sell", QTY, SPOT + 30)
    # 6 Oct weekly, ITM put closest to 150: 23550 (100 in the money + 60 = 160).
    assert (legs["option"]["symbol"], legs["option"]["side"], legs["option"]["entry_price"]) == ("NIFTY26100623550PE", "sell", 160.0)
    assert market.portfolio.cash == CASH + QTY * 160.0  # the premium only -- the future moves no cash
    assert "NIFTY 23,450.0 vs MA 23,500.0" in ctx._last_reason

    market.prices(at(1, 10, 10))
    held = await market.check(at(1, 10, 10), 1.04)  # PCR up to 1.04, NIFTY still under the MA: held
    assert held.state["position"] is not None and held._last_action == "hold" and "exit above 1.05" in held._last_reason

    # NIFTY falls 100: the short future gains 100/unit, the put costs 100 more.
    market.prices(at(1, 10, 14), spot=SPOT - 100)
    out = await market.check(at(1, 10, 14), 1.06)
    assert out.state["position"] is None and out._last_signal == "COVER"
    [trade] = await market.trades()
    assert trade.exit_reason == "pcr_above_1.05" and len(trade.legs) == 2
    assert trade.pnl == pytest.approx(QTY * 100 - QTY * 100)  # +65,000 future, -65,000 put
    assert market.portfolio.cash == pytest.approx(CASH + trade.pnl)


async def test_bullish_enters_above_110_over_the_ma_and_exits_below_095(market):
    await market.history(23400.0, at(1, 11, 0))  # MA 23400, NIFTY 23450 over it
    market.prices(at(1, 11, 0, 5))
    ctx = await market.check(at(1, 11, 0, 5), 1.20)
    legs = ctx.state["position"]["legs"]
    assert ctx._last_signal == "LONG_FUT_CE" and legs["future"]["side"] == "buy"
    assert (legs["option"]["symbol"], legs["option"]["entry_price"]) == ("NIFTY26100623350CE", 160.0)  # 100 ITM + 60

    market.prices(at(1, 11, 5), spot=SPOT + 40)
    assert (await market.check(at(1, 11, 5), 0.96)).state["position"] is not None

    market.prices(at(1, 11, 10), spot=SPOT + 40)
    out = await market.check(at(1, 11, 10), 0.94)
    [trade] = await market.trades()
    assert out.state["position"] is None and trade.exit_reason == "pcr_below_0.95"
    assert trade.pnl == pytest.approx(QTY * 40 - QTY * 40)  # long future +40/unit, short call -40/unit
    assert market.portfolio.cash == pytest.approx(CASH + trade.pnl)


async def test_no_entry_with_nifty_on_the_wrong_side_of_the_ma_or_without_an_ma(market):
    market.prices(at(1, 10, 0, 5))
    no_ma = await market.check(at(1, 10, 0, 5), 0.85)  # no candles at all
    assert no_ma.state.get("position") is None and "no MA: fewer than 8 NIFTY 15-min candles" in no_ma._last_reason

    await market.history(23400.0, at(1, 10, 0))  # MA 23400: NIFTY 23450 is over it
    wrong_side = await market.check(at(1, 10, 0, 6), 0.85)
    assert wrong_side.state.get("position") is None and "bearish waits for NIFTY below the MA" in wrong_side._last_reason

    market.prices(at(1, 11, 0))
    behind = await market.check(at(1, 11, 0), 1.20)  # the newest candle ended at 10:00: an hour behind
    assert behind.state.get("position") is None and "NIFTY 15-min candles behind" in behind._last_reason
    assert market.portfolio.cash == CASH


async def test_no_entry_outside_09_30_to_15_00_in_the_neutral_zone_or_without_both_prices(market):
    await market.history(23400.0, at(1, 10, 0))
    market.prices(at(1, 9, 29, 55))
    early = await market.check(at(1, 9, 29, 55), 1.20)
    assert early.state.get("position") is None and "entries 09:30-15:00" in early._last_reason
    market.prices(at(1, 10, 0))
    flat = await market.check(at(1, 10, 0), 1.00)
    assert flat.state.get("position") is None and "enters below 0.90 (NIFTY under the MA) or above 1.10 (over it)" in flat._last_reason
    assert (await market.check(at(1, 10, 1), None)).state.get("position") is None  # no PCR record

    market.prices(at(1, 10, 5), skip=(market.oct_fut,))
    missing = await market.check(at(1, 10, 5), 1.20)
    assert missing.state.get("position") is None and "no live price for NIFTY26OCTFUT" in missing._last_reason
    assert market.portfolio.cash == CASH  # neither leg opened


async def test_the_first_entry_is_at_0930_on_the_previous_sessions_ma(market):
    await market.history(23400.0, at(1, 9, 15))  # 30 Sep's candles only
    market.prices(at(1, 9, 30, 5))
    ctx = await market.check(at(1, 9, 30, 5), 1.20)
    assert ctx.state["position"]["bias"] == "bullish" and ctx.state["position"]["ma_at_entry"] == 23400.0


async def test_the_trailing_stop_closes_a_bullish_trade_on_a_15_min_close_below_the_ma_and_it_re_enters_after_one_above(market):
    await market.history(23400.0, at(1, 10, 0))  # MA 23400
    market.prices(at(1, 10, 0, 5))
    await market.check(at(1, 10, 0, 5), 1.20)

    market.prices(at(1, 10, 5), spot=23390.0)  # a dip under the MA mid-candle: held (15 Oct, closed at 14:24)
    dip = await market.check(at(1, 10, 5), 1.20)
    assert dip.state["position"] is not None and "stop on a 15-min close below it" in dip._last_reason

    await market.candles(at(1, 10, 0), at(1, 10, 0), 23380.0)  # 10:00-10:15 closes under the MA (23397.5): stopped
    market.prices(at(1, 10, 15, 40), spot=23390.0)
    stopped = await market.check(at(1, 10, 15, 40), 1.20)
    [trade] = await market.trades()
    assert stopped.state["position"] is None and trade.exit_reason == "ma_stop"
    assert "closed bullish (trailing stop: 10:15 close 23,380.0 vs MA 23,397.5)" in stopped._last_reason
    assert stopped.state["_ma_stop"]["bias"] == "bullish"

    market.prices(at(1, 10, 20))  # NIFTY back over the MA, but no 15-min candle has closed since the stop
    wait = await market.check(at(1, 10, 20), 1.20)
    assert wait.state.get("position") is None and "bullish stopped out at 10:15 -- waits for a 15-min close above the MA" in wait._last_reason

    await market.candles(at(1, 10, 15), at(1, 10, 15), 23480.0)  # 10:15-10:30 closes over the MA (23407.5): back in
    market.prices(at(1, 10, 30, 30))
    back = await market.check(at(1, 10, 30, 30), 1.20)
    assert back.state["position"]["bias"] == "bullish" and back._last_signal == "LONG_FUT_CE" and "_ma_stop" not in back.state


async def test_a_candle_that_closed_before_the_entry_never_stops_it(market):
    await market.history(23400.0, at(1, 9, 45))
    await market.candles(at(1, 9, 45), at(1, 9, 45), 23380.0)  # 09:45-10:00 closed under the MA (23397.5)
    market.prices(at(1, 10, 5))  # 10:05, NIFTY back over it: bullish
    await market.check(at(1, 10, 5), 1.20)
    market.prices(at(1, 10, 6))
    held = await market.check(at(1, 10, 6), 1.20)
    assert held.state["position"] is not None and "last 10:00 close 23,380.0 vs MA 23,397.5" in held._last_reason


async def test_the_trailing_stop_closes_a_bearish_trade_on_a_15_min_close_above_the_ma(market):
    await market.history(23500.0, at(1, 10, 0))  # MA 23500
    market.prices(at(1, 10, 0, 5))
    await market.check(at(1, 10, 0, 5), 0.85)
    market.prices(at(1, 10, 5), spot=23510.0)
    assert (await market.check(at(1, 10, 5), 0.85)).state["position"] is not None  # over the MA mid-candle: held

    await market.candles(at(1, 10, 0), at(1, 10, 0), 23520.0)  # closes over the MA (23502.5)
    market.prices(at(1, 10, 15, 40), spot=23510.0)
    out = await market.check(at(1, 10, 15, 40), 0.85)
    [trade] = await market.trades()
    assert out.state["position"] is None and trade.exit_reason == "ma_stop"
    assert trade.pnl == pytest.approx(-QTY * 60 + QTY * 60)  # short future -60/unit, the put 60 cheaper


async def test_a_jump_from_bearish_to_bullish_exits_and_enters_on_the_same_check(market):
    await market.history(23500.0, at(1, 10, 0))
    market.prices(at(1, 10, 0))
    await market.check(at(1, 10, 0), 0.85)
    market.prices(at(1, 10, 5), spot=23550.0)  # over the MA, PCR 1.20: the PCR exit, then bullish
    ctx = await market.check(at(1, 10, 5), 1.20)
    assert ctx.state["position"]["bias"] == "bullish" and ctx._last_signal == "LONG_FUT_CE"
    assert "closed bearish (PCR 1.200)" in ctx._last_reason
    assert [t.exit_reason for t in await market.trades()] == ["pcr_above_1.05"]


async def test_everything_closes_at_1500_and_nothing_enters_after(market):
    await market.history(23400.0, at(1, 14, 45))
    market.prices(at(1, 14, 0))
    held = await market.check(at(1, 14, 0), 1.20)
    assert held.state["position"] is not None and held._wake_at == at(1, 15, 0).astimezone(timezone.utc)

    market.prices(at(1, 15, 0))
    out = await market.check(at(1, 15, 0), 1.20)
    assert out.state["position"] is None and out._last_action == "exited" and "15:00 exit" in out._last_reason
    assert [t.exit_reason for t in await market.trades()] == ["square_off_1500"]
    market.prices(at(1, 15, 5))
    after = await market.check(at(1, 15, 5), 1.20)
    assert after.state.get("position") is None and "entries 09:30-15:00" in after._last_reason


async def test_a_position_from_an_earlier_day_is_closed_at_the_first_check(market):
    await market.history(23400.0, at(1, 11, 0))
    market.prices(at(1, 11, 0, 5))
    await market.check(at(1, 11, 0, 5), 1.20)  # held past 15:00 (the card was stopped then)
    market.prices(at(5, 9, 15, 10))
    out = await market.check(at(5, 9, 15, 10), 1.20)
    assert out.state["position"] is None and [t.exit_reason for t in await market.trades()] == ["carried_overnight"]


async def test_stop_and_exit_closes_both_legs(market):
    await market.history(23500.0, at(1, 10, 0))
    market.prices(at(1, 10, 0))
    await market.check(at(1, 10, 0), 0.85)
    market.deployment.state = {**market.deployment.state, "force_exit": True}
    market.prices(at(1, 10, 1))
    ctx = await market.check(at(1, 10, 1), 0.85)  # still bearish: closed because it was asked
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

    await market.history(23500.0, at(1, 10, 0))
    market.prices(at(1, 10, 0))
    await market.check(at(1, 10, 0), 0.85)
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
