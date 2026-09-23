import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from app.services.strategy.native_strategies.fo_opening_momentum import (
    _max_oi_strike_near_spot,
    _total_oi_pct_change,
    evaluate,
    in_expiry_blackout,
    momentum_direction,
    nifty_filter_allows,
    passes_momentum,
    passes_oi_change,
    passes_retracement,
    retracement_pct,
    sma_exit_triggered,
)


def test_momentum_direction_and_threshold():
    assert momentum_direction(3.0) == "CE"
    assert momentum_direction(-3.0) == "PE"
    assert passes_momentum(2.01) is True
    assert passes_momentum(2.0) is False
    assert passes_momentum(-2.5) is True


def test_oi_change():
    assert passes_oi_change(7.1) is True
    assert passes_oi_change(-7.1) is True
    assert passes_oi_change(7.0) is False
    assert passes_oi_change(None) is False


def test_retracement():
    # CE: retraces from the High. Half-range retrace == exactly 50% -> fails (>= boundary)
    assert retracement_pct(110.0, 100.0, 105.0, "CE") == 50.0
    assert passes_retracement(110.0, 100.0, 105.0, "CE") is False
    assert passes_retracement(110.0, 100.0, 106.0, "CE") is True  # < 50%
    # PE: retraces from the Low
    assert retracement_pct(110.0, 100.0, 105.0, "PE") == 50.0
    assert passes_retracement(110.0, 100.0, 104.0, "PE") is True
    # doji (no range) -> 0% retracement, always passes
    assert retracement_pct(100.0, 100.0, 100.0, "CE") == 0.0


def test_nifty_filter():
    assert nifty_filter_allows("CE", nifty_open=100, nifty_close=101) is True  # green -> gainers ok
    assert nifty_filter_allows("PE", nifty_open=100, nifty_close=101) is True  # green -> losers ok too
    assert nifty_filter_allows("CE", nifty_open=101, nifty_close=100) is False  # red -> gainers excluded
    assert nifty_filter_allows("PE", nifty_open=101, nifty_close=100) is True  # red -> losers still ok


def test_sma_exit():
    # 8-period SMA, need 2 consecutive closes below it for a CE exit
    rising = [100 + i for i in range(9)]  # closes above their own trailing SMA throughout
    assert sma_exit_triggered("CE", rising) is False
    falling_then_below = [100, 101, 102, 103, 104, 105, 106, 107, 90, 88]
    assert sma_exit_triggered("CE", falling_then_below) is True
    assert sma_exit_triggered("PE", falling_then_below) is False
    # not enough candles yet
    assert sma_exit_triggered("CE", [100, 99, 98]) is False


def test_expiry_blackout_window():
    expiry = date(2026, 9, 29)  # Tuesday
    assert in_expiry_blackout(date(2026, 9, 29), expiry) is True  # expiry day itself
    assert in_expiry_blackout(date(2026, 9, 25), expiry) is True  # 2 trading days before (Fri)
    assert in_expiry_blackout(date(2026, 10, 1), expiry) is True  # 2 trading days after (Thu)
    assert in_expiry_blackout(date(2026, 9, 22), expiry) is False  # a full week before, clear
    assert in_expiry_blackout(date(2026, 10, 6), expiry) is False  # a week after, clear


async def test_max_oi_strike_near_spot_picks_the_highest_oi_within_band(db_session: AsyncSession):
    equity = Instrument(exchange="NSE", symbol="OICO", name="OI Co", instrument_type="equity", data_source="zerodha_kite", external_ref="OICO")
    db_session.add(equity)
    await db_session.flush()
    expiry = date(2026, 9, 29)

    def _option(strike, oi):
        opt = Instrument(
            exchange="NFO", symbol=f"OICO26SEP{int(strike)}CE", name="OICO CE", instrument_type="option", data_source="zerodha_kite",
            external_ref=f"OICO26SEP{int(strike)}CE", strike=strike, option_type="CE", expiry=expiry, lot_size=500,
            underlying_instrument_id=equity.id,
        )
        db_session.add(opt)
        return opt

    near_low = _option(95.0, 2000)
    near_high_max = _option(105.0, 9000)  # highest OI, within the +/-10% band of spot=100
    far_outside_band = _option(120.0, 50000)  # far higher OI, but outside the band -- must be ignored
    await db_session.flush()

    ts = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
    db_session.add_all([
        OhlcvCandle(instrument_id=near_low.id, timeframe="5m", ts=ts, open=1, high=1, low=1, close=1, volume=0, open_interest=2000, source="test"),
        OhlcvCandle(instrument_id=near_high_max.id, timeframe="5m", ts=ts, open=1, high=1, low=1, close=1, volume=0, open_interest=9000, source="test"),
        OhlcvCandle(instrument_id=far_outside_band.id, timeframe="5m", ts=ts, open=1, high=1, low=1, close=1, volume=0, open_interest=50000, source="test"),
    ])
    await db_session.commit()

    ctx = NativeContext(db=db_session, portfolio=None, deployment=None, state={})
    result = await _max_oi_strike_near_spot(ctx, equity.id, expiry, spot=100.0, option_type="CE")
    assert result == (105.0, 9000.0)


async def test_total_oi_pct_change_sums_futures_and_every_strike(db_session: AsyncSession):
    """Total OI must be futures + every CE strike + every PE strike for
    the same expiry -- demonstrated here by a case where the futures leg
    alone only moves 2% (would fail the >7% threshold on its own), but
    the combined total clears it because of the options' OI change."""
    equity = Instrument(exchange="NSE", symbol="TOICO", name="Total OI Co", instrument_type="equity", data_source="zerodha_kite", external_ref="TOICO")
    db_session.add(equity)
    await db_session.flush()
    expiry = date(2026, 9, 29)
    future = Instrument(
        exchange="NFO", symbol="TOICO26SEPFUT", name="TOICO FUT", instrument_type="future", data_source="zerodha_kite",
        external_ref="TOICO26SEPFUT", expiry=expiry, lot_size=500, underlying_instrument_id=equity.id,
    )
    db_session.add(future)
    await db_session.flush()

    def _option(strike, otype):
        opt = Instrument(
            exchange="NFO", symbol=f"TOICO26SEP{int(strike)}{otype}", name="x", instrument_type="option", data_source="zerodha_kite",
            external_ref=f"TOICO26SEP{int(strike)}{otype}", strike=strike, option_type=otype, expiry=expiry, lot_size=500,
            underlying_instrument_id=equity.id,
        )
        db_session.add(opt)
        return opt

    ce1 = _option(100.0, "CE")
    pe1 = _option(90.0, "PE")
    await db_session.flush()

    prev_ts = datetime(2026, 9, 18, 0, 0, tzinfo=IST)
    today = date(2026, 9, 21)
    latest_ts = datetime.combine(today, dtime(9, 15), tzinfo=IST)

    db_session.add(_candle(future.id, prev_ts, 1, 1, 1, 1, oi=100000, timeframe="1d"))
    db_session.add(_candle(future.id, latest_ts, 1, 1, 1, 1, oi=102000))  # futures alone: +2%
    db_session.add(_candle(ce1.id, prev_ts, 1, 1, 1, 1, oi=20000, timeframe="1d"))
    db_session.add(_candle(ce1.id, latest_ts, 1, 1, 1, 1, oi=30000))
    db_session.add(_candle(pe1.id, prev_ts, 1, 1, 1, 1, oi=10000, timeframe="1d"))
    db_session.add(_candle(pe1.id, latest_ts, 1, 1, 1, 1, oi=15000))
    await db_session.commit()

    ctx = NativeContext(db=db_session, portfolio=None, deployment=None, state={})
    result = await _total_oi_pct_change(ctx, equity.id, future.id, expiry, today)

    assert result is not None
    assert (result["fut_prev"], result["fut_latest"]) == (100000, 102000)
    assert (result["ce_prev"], result["ce_latest"]) == (20000, 30000)
    assert (result["pe_prev"], result["pe_latest"]) == (10000, 15000)
    assert result["prev_total"] == 130000  # 100000 + 20000 + 10000
    assert result["latest_total"] == 147000  # 102000 + 30000 + 15000

    futures_only_pct = (102000 - 100000) / 100000 * 100.0
    assert not passes_oi_change(futures_only_pct)  # 2% alone would fail the >7% threshold
    assert passes_oi_change(result["pct_change"])  # but the combined Total OI change clears it


async def _setup(db_session: AsyncSession, *, cash: float = 1000000.0):
    role = Role(name=f"native_fo_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"native_fo_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Native FO User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    strategy = Strategy(name="F&O Opening Momentum", owner_id=user.id, code_type="native")
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="5m", instrument_ids=[], parameters={},
        entry_rules=None, exit_rules=None, python_code="# see native_strategies/fo_opening_momentum.py",
        position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={},
    )
    db_session.add(version)
    await db_session.flush()

    portfolio = PaperPortfolio(user_id=user.id, cash=cash, initial_capital=cash)
    db_session.add(portfolio)
    await db_session.flush()

    deployment = PaperNativeDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
        status=DeploymentStatus.ACTIVE.value, state=None,
    )
    db_session.add(deployment)

    nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="Nifty 50", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    equity = Instrument(exchange="NSE", symbol="TESTCO", name="Test Co", instrument_type="equity", data_source="zerodha_kite", external_ref="TESTCO")
    db_session.add_all([nifty, equity])
    await db_session.flush()

    future = Instrument(
        exchange="NFO", symbol="TESTCO26SEPFUT", name="TESTCO FUT", instrument_type="future", data_source="zerodha_kite",
        external_ref="TESTCO26SEPFUT", expiry=date(2026, 9, 29), lot_size=500, underlying_instrument_id=equity.id,
    )
    db_session.add(future)
    await db_session.flush()

    return {"user": user, "portfolio": portfolio, "deployment": deployment, "nifty": nifty, "equity": equity, "future": future}


def _candle(instrument_id, ts_ist: datetime, o, h, l, c, oi=None, timeframe="5m"):
    return OhlcvCandle(
        instrument_id=instrument_id, timeframe=timeframe, ts=ts_ist.astimezone(timezone.utc),
        open=o, high=h, low=l, close=c, volume=0, open_interest=oi, source="test",
    )


async def test_scan_shortlists_and_enters_on_breakout_then_exits_on_sma(db_session: AsyncSession):
    ctx_data = await _setup(db_session)
    equity, nifty, future = ctx_data["equity"], ctx_data["nifty"], ctx_data["future"]
    day = date(2026, 9, 21)  # Monday
    prev_day = date(2026, 9, 18)

    # previous session's close (for % move) and EOD OI (for the OI filter --
    # the daily "1d" candle, the real EOD reading, not a 5m bar)
    db_session.add(_candle(equity.id, datetime.combine(prev_day, dtime(15, 25), tzinfo=IST), 95, 96, 94, 95.0))
    db_session.add(_candle(future.id, datetime.combine(prev_day, dtime(0, 0), tzinfo=IST), 95, 96, 94, 95.0, oi=100000, timeframe="1d"))

    # 9:15-9:20 opening candle: strong CE setup -- open==low, closes near the high (no retracement)
    db_session.add(_candle(equity.id, datetime.combine(day, dtime(9, 15), tzinfo=IST), 100.0, 102.0, 100.0, 101.9))
    # 9:20-9:25: extends the range up to the 9:25 breakout level
    db_session.add(_candle(equity.id, datetime.combine(day, dtime(9, 20), tzinfo=IST), 101.9, 103.0, 101.5, 102.5))
    # today's latest futures OI, +8% vs prior session -> passes Step 3
    db_session.add(_candle(future.id, datetime.combine(day, dtime(9, 15), tzinfo=IST), 95, 96, 94, 95.0, oi=108000))

    # Nifty green 9:15-9:20 -> gainers allowed
    db_session.add(_candle(nifty.id, datetime.combine(day, dtime(9, 15), tzinfo=IST), 25000, 25050, 24990, 25040))

    await db_session.commit()

    tick_engine.set_real_price(equity.id, 103.5, "test")  # +9% vs prev close 95 -> passes momentum

    # ---- 9:20 scan ----
    ctx = NativeContext(db=db_session, portfolio=ctx_data["portfolio"], deployment=ctx_data["deployment"], state={}, now=datetime.combine(day, dtime(9, 20), tzinfo=IST))
    await evaluate(ctx)
    assert "TESTCO" in ctx.state["setups"]
    setup = ctx.state["setups"]["TESTCO"]
    assert setup["direction"] == "CE"
    assert setup["status"] == "watching"

    # ---- 9:25 breakout mark + entry (spot already above the 9:15-9:25 high of 103.0) ----
    option = Instrument(
        exchange="NFO", symbol="TESTCO26SEP106CE", name="TESTCO 106 CE", instrument_type="option", data_source="zerodha_kite",
        external_ref="TESTCO26SEP106CE", strike=106.0, option_type="CE", expiry=date(2026, 9, 29), lot_size=500,
        underlying_instrument_id=equity.id,
    )
    db_session.add(option)
    await db_session.flush()
    tick_engine.set_real_price(option.id, 5.0, "test")

    ctx.now = datetime.combine(day, dtime(9, 30), tzinfo=IST)
    starting_cash = ctx.portfolio.cash
    await evaluate(ctx)
    setup = ctx.state["setups"]["TESTCO"]
    assert setup["status"] == "triggered"
    assert setup["option_symbol"] == "TESTCO26SEP106CE"
    assert setup["entry_premium"] == 5.0
    assert ctx.portfolio.cash == starting_cash - 5.0 * 500  # bought 1 lot

    # ---- SMA exit: seed a falling run of 5m closes on the underlying, then check ----
    closes = [102, 101, 100, 99, 98, 97, 96, 95, 80, 78]
    candle_start = datetime.combine(day, dtime(9, 35), tzinfo=IST)
    for i, c in enumerate(closes):
        ts = candle_start + timedelta(minutes=5 * i)
        db_session.add(_candle(equity.id, ts, c - 1, c + 1, c - 1, c))
    await db_session.commit()
    tick_engine.set_real_price(option.id, 1.0, "test")

    ctx.now = datetime.combine(day, dtime(11, 0), tzinfo=IST)
    await evaluate(ctx)
    setup = ctx.state["setups"]["TESTCO"]
    assert setup["status"] == "exited"
    assert setup["exit_premium"] == 1.0
    assert setup["pnl"] == (1.0 - 5.0) * 500

    # ---- 3:10 report ----
    ctx.now = datetime.combine(day, dtime(15, 10), tzinfo=IST)
    await evaluate(ctx)
    assert ctx.state["report_sent"] is True


async def test_second_scan_at_925_adds_newly_qualifying_stock_without_disturbing_the_first(db_session: AsyncSession):
    """A stock that doesn't clear the 2% momentum threshold at the 9:20
    pass but does by 9:25 (its live price kept moving) gets added on the
    second scan -- per instruction, the 9:25 pass only ever adds newly-
    qualifying symbols, never re-validates or removes what already
    passed at 9:20."""
    ctx_data = await _setup(db_session)
    equity, nifty, future = ctx_data["equity"], ctx_data["nifty"], ctx_data["future"]
    day = date(2026, 9, 21)  # Monday
    prev_day = date(2026, 9, 18)

    late_equity = Instrument(
        exchange="NSE", symbol="LATECO", name="Late Co", instrument_type="equity", data_source="zerodha_kite", external_ref="LATECO",
    )
    db_session.add(late_equity)
    await db_session.flush()
    late_future = Instrument(
        exchange="NFO", symbol="LATECO26SEPFUT", name="LATECO FUT", instrument_type="future", data_source="zerodha_kite",
        external_ref="LATECO26SEPFUT", expiry=date(2026, 9, 29), lot_size=500, underlying_instrument_id=late_equity.id,
    )
    db_session.add(late_future)
    await db_session.flush()

    db_session.add(_candle(nifty.id, datetime.combine(day, dtime(9, 15), tzinfo=IST), 25000, 25050, 24990, 25040))

    # TESTCO: qualifies cleanly at 9:20 (same setup as the main scan test).
    db_session.add(_candle(equity.id, datetime.combine(prev_day, dtime(15, 25), tzinfo=IST), 95, 96, 94, 95.0))
    db_session.add(_candle(future.id, datetime.combine(prev_day, dtime(0, 0), tzinfo=IST), 95, 96, 94, 95.0, oi=100000, timeframe="1d"))
    db_session.add(_candle(equity.id, datetime.combine(day, dtime(9, 15), tzinfo=IST), 100.0, 102.0, 100.0, 101.9))
    db_session.add(_candle(future.id, datetime.combine(day, dtime(9, 15), tzinfo=IST), 95, 96, 94, 95.0, oi=108000))
    tick_engine.set_real_price(equity.id, 103.5, "test")  # +9% vs prev close 95

    # LATECO: identical OI/retracement setup, but its live price is only
    # +1.5% (below the 2% threshold) at 9:20.
    db_session.add(_candle(late_equity.id, datetime.combine(prev_day, dtime(15, 25), tzinfo=IST), 200, 201, 199, 200.0))
    db_session.add(_candle(late_future.id, datetime.combine(prev_day, dtime(0, 0), tzinfo=IST), 200, 201, 199, 200.0, oi=50000, timeframe="1d"))
    db_session.add(_candle(late_equity.id, datetime.combine(day, dtime(9, 15), tzinfo=IST), 200.0, 204.0, 200.0, 203.8))
    db_session.add(_candle(late_future.id, datetime.combine(day, dtime(9, 15), tzinfo=IST), 200, 201, 199, 200.0, oi=54000))  # +8% -- passes Step 3 throughout
    tick_engine.set_real_price(late_equity.id, 203.0, "test")  # +1.5% vs prev close 200 -- below threshold at 9:20

    await db_session.commit()

    ctx = NativeContext(db=db_session, portfolio=ctx_data["portfolio"], deployment=ctx_data["deployment"], state={}, now=datetime.combine(day, dtime(9, 20), tzinfo=IST))
    await evaluate(ctx)
    assert "TESTCO" in ctx.state["setups"]
    assert "LATECO" not in ctx.state["setups"]

    # LATECO's price keeps moving and clears the threshold by 9:25.
    tick_engine.set_real_price(late_equity.id, 206.0, "test")  # +3% vs prev close 200

    ctx.now = datetime.combine(day, dtime(9, 25), tzinfo=IST)
    await evaluate(ctx)

    assert ctx.state["second_scan_done"] is True
    assert "LATECO" in ctx.state["setups"]
    assert ctx.state["setups"]["LATECO"]["direction"] == "CE"
    assert "TESTCO" in ctx.state["setups"]  # untouched by the second pass


# ---------------------------------------------------------------------
# Live Kite data path -- the production situation at 9:20: a freshly
# restarted process (TickEngine has no price for any stock yet) and
# nothing in ohlcv_candles for today, since no scheduler keeps a native
# strategy's 5m candles or futures OI current. Only yesterday's EOD OI is
# stored.
# ---------------------------------------------------------------------
class _FakeKite:
    def __init__(self, quotes: dict, bars: dict):
        self.quotes = quotes
        self.bars = bars  # {(tradingsymbol, timeframe): [bar, ...]}
        self.historical_calls: list[tuple[str, str]] = []

    async def get_quote_batch(self, keys):
        return {key: self.quotes[key] for key in keys if key in self.quotes}

    async def get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        self.historical_calls.append((symbol, timeframe))
        return [bar for bar in self.bars.get((symbol, timeframe), []) if start <= bar["ts"] <= end]

    async def get_ltp(self, exchange, tradingsymbol):
        return {"price": self.quotes[f"{exchange}:{tradingsymbol}"]["last_price"]}


def _bar(ts_ist: datetime, o, h, l, c):
    return {"ts": ts_ist, "open": o, "high": h, "low": l, "close": c, "volume": 1000, "open_interest": None}


async def _setup_360one(db_session: AsyncSession):
    """360ONE with a current-month future and one CE/PE strike, plus
    yesterday's EOD OI for every leg -- and a stale stored close from
    yesterday, which is all the old DB-only read path had to go on."""
    ctx_data = await _setup(db_session)
    equity = Instrument(exchange="NSE", symbol="360ONE", name="360 ONE WAM", instrument_type="equity", data_source="zerodha_kite", external_ref="360ONE")
    db_session.add(equity)
    await db_session.flush()
    expiry = date(2026, 9, 29)
    future = Instrument(
        exchange="NFO", symbol="360ONE26SEPFUT", name="360ONE FUT", instrument_type="future", data_source="zerodha_kite",
        external_ref="360ONE26SEPFUT", expiry=expiry, lot_size=500, underlying_instrument_id=equity.id,
    )
    ce = Instrument(
        exchange="NFO", symbol="360ONE26SEP1060CE", name="x", instrument_type="option", data_source="zerodha_kite",
        external_ref="360ONE26SEP1060CE", strike=1060.0, option_type="CE", expiry=expiry, lot_size=500, underlying_instrument_id=equity.id,
    )
    pe = Instrument(
        exchange="NFO", symbol="360ONE26SEP960PE", name="x", instrument_type="option", data_source="zerodha_kite",
        external_ref="360ONE26SEP960PE", strike=960.0, option_type="PE", expiry=expiry, lot_size=500, underlying_instrument_id=equity.id,
    )
    db_session.add_all([future, ce, pe])
    await db_session.flush()

    yesterday = date(2026, 9, 22)
    eod = datetime.combine(yesterday, dtime(0, 0), tzinfo=IST)
    db_session.add(_candle(equity.id, datetime.combine(yesterday, dtime(15, 25), tzinfo=IST), 1001, 1002, 999, 1000.0))
    db_session.add(_candle(future.id, eod, 1, 1, 1, 1, oi=100000, timeframe="1d"))
    db_session.add(_candle(ce.id, eod, 1, 1, 1, 1, oi=20000, timeframe="1d"))
    db_session.add(_candle(pe.id, eod, 1, 1, 1, 1, oi=10000, timeframe="1d"))
    await db_session.commit()
    return {**ctx_data, "one": equity, "one_future": future, "one_ce": ce, "one_pe": pe}


def _fake_kite_for_360one(today: date, *, nifty_green: bool = True) -> _FakeKite:
    at = lambda h, m: datetime.combine(today, dtime(h, m), tzinfo=IST)  # noqa: E731
    nifty_bar = _bar(at(9, 15), 25000, 25050, 24990, 25040) if nifty_green else _bar(at(9, 15), 25040, 25050, 24950, 24960)
    return _FakeKite(
        quotes={
            "NSE:360ONE": {"last_price": 1035.0, "ohlc": {"open": 1002.0, "high": 1036.0, "low": 1001.0, "close": 1000.0}},
            "NSE:TESTCO": {"last_price": 100.5, "ohlc": {"close": 100.0}},  # +0.5% -- below the 2% gate
            "NFO:360ONE26SEPFUT": {"last_price": 1040.0, "oi": 108000},
            "NFO:360ONE26SEP1060CE": {"last_price": 12.0, "oi": 25000},
            "NFO:360ONE26SEP960PE": {"last_price": 3.0, "oi": 11000},
        },
        bars={
            ("NIFTY 50", "5m"): [nifty_bar],
            ("360ONE", "5m"): [
                _bar(at(9, 15), 1002.0, 1036.0, 1001.0, 1034.0),
                _bar(at(9, 20), 1034.0, 1037.0, 1033.0, 1035.0),  # still forming at 9:20:05 -- must not be stored
            ],
        },
    )


async def _alert_titles(db_session: AsyncSession) -> list[str]:
    from sqlalchemy import select as sa_select

    from app.models.alert import Alert

    return list((await db_session.execute(sa_select(Alert.title))).scalars().all())


async def test_live_scan_catches_a_stock_whose_data_was_never_stored(db_session: AsyncSession, monkeypatch):
    from sqlalchemy import select as sa_select

    import app.services.strategy.native_strategies.fo_opening_momentum as fo

    data = await _setup_360one(db_session)
    today = date(2026, 9, 23)
    kite = _fake_kite_for_360one(today)
    monkeypatch.setattr(fo, "_live_broker", lambda ctx: _async_value(kite))
    monkeypatch.setattr(fo, "KITE_HISTORICAL_PACING_SECONDS", 0)
    monkeypatch.setattr(fo, "KITE_QUOTE_PACING_SECONDS", 0)

    ctx = NativeContext(
        db=db_session, portfolio=data["portfolio"], deployment=data["deployment"], state={},
        now=datetime.combine(today, dtime(9, 20, 5), tzinfo=IST),
    )
    await evaluate(ctx)

    assert ctx.state["shortlist_done"] is True
    setup = ctx.state["setups"]["360ONE"]
    assert setup["direction"] == "CE"
    assert round(setup["pct_change"], 2) == 3.5  # Kite quote: 1000 -> 1035, not the stale stored close
    # Total OI 130,000 (yesterday) -> 144,000 (live) = +10.77%
    assert round(setup["oi_pct_change"], 2) == 10.77
    assert setup["oi_detail"]["ce_counted"] == 1 and setup["oi_detail"]["pe_counted"] == 1

    titles = await _alert_titles(db_session)
    assert "F&O Opening Momentum: 360ONE shortlisted at 9:20" in titles
    assert "F&O Opening Momentum: 1 shortlisted at 9:20" in titles  # the scan summary
    assert ctx.state["scan_log"]["9:20"]["counts"]["below_momentum"] == 1  # TESTCO

    stored = (
        await db_session.execute(
            sa_select(OhlcvCandle.ts).where(OhlcvCandle.instrument_id == data["one"].id, OhlcvCandle.timeframe == "5m", OhlcvCandle.ts >= fo._ist_to_utc(today, dtime(9, 15)))
        )
    ).scalars().all()
    assert len(stored) == 1  # only the completed 9:15 candle, not the still-forming 9:20 one
    assert tick_engine.get_current_price(data["one"].id) == 1035.0


async def test_live_scan_waits_for_nifty_candle_then_reports_why_a_stock_was_rejected(db_session: AsyncSession, monkeypatch):
    import app.services.strategy.native_strategies.fo_opening_momentum as fo

    data = await _setup_360one(db_session)
    today = date(2026, 9, 23)
    kite = _fake_kite_for_360one(today, nifty_green=False)
    nifty_bars = kite.bars.pop(("NIFTY 50", "5m"))  # not published by Kite yet
    monkeypatch.setattr(fo, "_live_broker", lambda ctx: _async_value(kite))
    monkeypatch.setattr(fo, "KITE_HISTORICAL_PACING_SECONDS", 0)
    monkeypatch.setattr(fo, "KITE_QUOTE_PACING_SECONDS", 0)

    ctx = NativeContext(
        db=db_session, portfolio=data["portfolio"], deployment=data["deployment"], state={},
        now=datetime.combine(today, dtime(9, 20, 2), tzinfo=IST),
    )
    await evaluate(ctx)
    assert ctx.state["shortlist_done"] is False  # retried next tick, not marked done with nothing
    assert await _alert_titles(db_session) == []

    kite.bars[("NIFTY 50", "5m")] = nifty_bars
    ctx.now = datetime.combine(today, dtime(9, 20, 12), tzinfo=IST)
    await evaluate(ctx)

    assert ctx.state["shortlist_done"] is True
    assert "360ONE" not in ctx.state["setups"]
    rejected = ctx.state["scan_log"]["9:20"]["rejected"]
    assert [r["symbol"] for r in rejected] == ["360ONE"]
    assert rejected[0]["reasons"] == ["Nifty 9:15-9:20 candle red -- gainers excluded"]
    assert "F&O Opening Momentum: 0 shortlisted at 9:20" in await _alert_titles(db_session)


async def _async_value(value):
    return value


async def test_telegram_send_failure_never_raises(monkeypatch):
    import httpx
    from types import SimpleNamespace

    from app.services.notifications import telegram

    monkeypatch.setattr(telegram, "get_settings", lambda: SimpleNamespace(telegram_bot_token="t", telegram_chat_id="1"))

    async def _boom(self, *args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx.AsyncClient, "post", _boom)
    await telegram.send_telegram("subject", "body")  # must not raise
