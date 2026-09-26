"""FLY OI SCN v6 (native_strategies/fo_opening_momentum.py): the rules as
plain functions, then whole trading days against a fake Kite -- 09:20:07
watchlist, 09:25:07 Nifty bias and shortlist, breakout entry, SMA exit."""

import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.fo_scan import MARK_CLOSE, FoOiSnapshot, FoScanResult
from app.models.instrument import Instrument
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.broker.zerodha_broker import IST
from app.services.fo_scan import oi_store, pacing
from app.services.paper_trading.native_runner import NativeContext
from app.services.strategy.native_strategies import fo_opening_momentum as fo

DAY = date(2026, 10, 14)  # Wednesday
PREV = date(2026, 10, 13)
OCT_EXPIRY = date(2026, 10, 27)  # last Tuesday
NOV_EXPIRY = date(2026, 11, 23)  # last Tuesday (24th) is a holiday
STRIKES = [96.0, 98.0, 100.0, 102.0, 104.0, 106.0, 108.0, 110.0]


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    monkeypatch.setattr(pacing.quote_pacer, "interval", 0)
    monkeypatch.setattr(pacing.history_pacer, "interval", 0)


def _ist(d: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime.combine(d, dtime(h, m, s), tzinfo=IST)


# ---------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------
def test_move_and_oi_rules():
    assert fo.momentum_direction(2.5) == "CE" and fo.momentum_direction(-2.5) == "PE"
    assert fo.passes_momentum(2.01) and fo.passes_momentum(-2.01) and not fo.passes_momentum(2.0)
    # Only a rise counts.
    assert fo.passes_oi_rise(7.01) and not fo.passes_oi_rise(7.0)
    assert not fo.passes_oi_rise(-9.0) and not fo.passes_oi_rise(None)


def test_retracement_and_nifty_bias():
    assert fo.retracement_pct(104, 100, 103.5, "CE") == pytest.approx(12.5)
    assert not fo.passes_retracement(104, 100, 102, "CE")  # gave back exactly half
    assert fo.retracement_pct(100, 96, 99, "PE") == pytest.approx(75.0)
    assert fo.nifty_bias(25000, 25040) == "green" and fo.nifty_bias(25000, 25000) == "red"
    assert fo.nifty_allows("CE", "green") and fo.nifty_allows("PE", "green")
    assert not fo.nifty_allows("CE", "red") and fo.nifty_allows("PE", "red")


def test_expiry_blackout_counts_trading_days_before_and_after():
    assert fo.monthly_expiry(2026, 10) == OCT_EXPIRY
    assert fo.monthly_expiry(2026, 11) == NOV_EXPIRY  # Guru Nanak Jayanti moves it a day earlier
    assert fo.last_monthly_expiry_before(DAY) == date(2026, 9, 29)
    sep = date(2026, 9, 29)
    assert fo.in_expiry_blackout(OCT_EXPIRY, OCT_EXPIRY, sep)  # expiry day
    assert fo.in_expiry_blackout(date(2026, 10, 23), OCT_EXPIRY, sep)  # Fri: Mon + Tue left
    assert not fo.in_expiry_blackout(date(2026, 10, 22), OCT_EXPIRY, sep)
    assert fo.in_expiry_blackout(date(2026, 10, 1), OCT_EXPIRY, sep)  # 2nd trading day after
    assert not fo.in_expiry_blackout(date(2026, 10, 5), OCT_EXPIRY, sep)  # 2 Oct is a holiday: 3rd
    assert not fo.in_expiry_blackout(DAY, OCT_EXPIRY, sep)
    assert fo.in_expiry_blackout(date(2026, 11, 19), NOV_EXPIRY, OCT_EXPIRY)


def test_sma_exit_needs_two_closes_after_the_entry():
    yesterday = [{"ts": _ist(PREV, 14, 15) + timedelta(minutes=5 * i), "close": 100.0} for i in range(15)]
    today_closes = [103.5, 104.5, 105.6, 106.0, 106.5, 95.0, 94.0]
    today = [{"ts": _ist(DAY, 9, 15) + timedelta(minutes=5 * i), "close": c} for i, c in enumerate(today_closes)]
    entered = _ist(DAY, 9, 26)
    assert fo.sma_exit_due("CE", yesterday + today, entered)  # 9:40 and 9:45 closed below the SMA
    assert not fo.sma_exit_due("CE", yesterday + today[:-1], entered)  # only one so far
    # Two closes below the SMA that both closed before the entry don't count.
    assert not fo.sma_exit_due("CE", yesterday + today, _ist(DAY, 9, 46))
    assert fo.sma_exit_due("PE", yesterday + [{"ts": t["ts"], "close": 200 - t["close"]} for t in today], entered)


def test_total_oi_adds_future_ce_and_pe_and_skips_half_known_contracts():
    u = uuid.uuid4()

    def leg(kind, strike=None):
        return oi_store.Contract(uuid.uuid4(), u, "X", kind, OCT_EXPIRY, strike, "NFO", f"X{kind}{strike}")

    fut, ce, pe, new = leg("FUT"), leg("CE", 100.0), leg("PE", 100.0), leg("CE", 110.0)
    prev = {fut.instrument_id: 100000.0, ce.instrument_id: 20000.0, pe.instrument_id: 10000.0}
    now = {fut.instrument_id: 102000.0, ce.instrument_id: 30000.0, pe.instrument_id: 15000.0, new.instrument_id: 5000.0}
    oi = fo.total_oi([fut, ce, pe, new], prev, now)
    assert (oi["prev_total"], oi["now_total"]) == (130000.0, 147000.0)
    assert oi["pct_change"] == pytest.approx(13.0769, rel=1e-4)
    assert (oi["counted"], oi["listed"], oi["ce_counted"], oi["ce_listed"]) == (3, 4, 1, 2)
    assert fo.total_oi([fut], {}, now) is None


# ---------------------------------------------------------------------
# A day against a fake Kite
# ---------------------------------------------------------------------
class FakeKite:
    def __init__(self):
        self.quotes: dict[str, dict] = {}
        self.bars: dict[tuple[str, str], list[dict]] = {}
        self.requests: list[tuple] = []

    async def get_quote_batch(self, keys):
        self.requests.append(("quote", len(keys)))
        return {k: self.quotes[k] for k in keys if k in self.quotes}

    async def get_historical_data(self, symbol, timeframe, start, end, segment="NSE", instrument_token=None):
        self.requests.append(("history", symbol, timeframe))
        return [b for b in self.bars.get((symbol, timeframe), []) if start <= b["ts"] <= end]

    async def get_instruments(self, segment):
        self.requests.append(("instruments", segment))
        return []

    def bar(self, symbol, ts, o, h, low, c, timeframe="5m", oi=None):
        self.bars.setdefault((symbol, timeframe), []).append({"ts": ts, "open": o, "high": h, "low": low, "close": c, "volume": 0, "open_interest": oi})


async def _deployment(db: AsyncSession):
    role = Role(name=f"fo_{uuid.uuid4().hex[:6]}", description="x")
    db.add(role)
    await db.flush()
    user = User(email=f"fo_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="FO")
    user.user_roles = [UserRole(role=role)]
    db.add(user)
    await db.flush()
    strategy = Strategy(name="FLY OI SCN", owner_id=user.id, code_type="native")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="5m", instrument_ids=[], parameters={},
        python_code="# fo_opening_momentum.py", position_sizing={}, risk_rules={},
    )
    portfolio = PaperPortfolio(user_id=user.id, cash=1_000_000.0, initial_capital=1_000_000.0)
    db.add_all([version, portfolio])
    await db.flush()
    deployment = PaperNativeDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id, status=DeploymentStatus.ACTIVE.value,
    )
    db.add(deployment)
    await db.flush()
    return portfolio, deployment


async def _stock(db: AsyncSession, symbol: str) -> dict:
    """An F&O stock: its equity, October + November futures, and October
    (current month) CE/PE strikes, plus one November strike."""
    eq = Instrument(exchange="NSE", symbol=symbol, name=symbol, instrument_type="equity", data_source="zerodha_kite", external_ref=symbol)
    db.add(eq)
    await db.flush()

    def nfo(ref, kind, expiry, strike=None, option_type=None):
        return Instrument(
            exchange="NFO", symbol=ref, name=ref, instrument_type=kind, data_source="zerodha_kite", external_ref=ref,
            expiry=expiry, strike=strike, option_type=option_type, lot_size=500, underlying_instrument_id=eq.id,
        )

    fut = nfo(f"{symbol}26OCTFUT", "future", OCT_EXPIRY)
    nov_fut = nfo(f"{symbol}26NOVFUT", "future", NOV_EXPIRY)
    options = [nfo(f"{symbol}26OCT{int(k)}{t}", "option", OCT_EXPIRY, k, t) for k in STRIKES for t in ("CE", "PE")]
    nov_option = nfo(f"{symbol}26NOV100CE", "option", NOV_EXPIRY, 100.0, "CE")
    db.add_all([fut, nov_fut, nov_option, *options])
    await db.flush()
    return {"eq": eq, "fut": fut, "options": options, "legs": [fut, *options]}


async def _world(db: AsyncSession, kite: FakeKite, *, nifty_green=True, oi_rise=(1.10, 1.08, 1.0)):
    """NIFTY + GAINCO (+4%, CE), LOSECO (-5%, PE), FLATCO (+1%), with
    yesterday's closing OI in the store and today's in the fake quotes."""
    portfolio, deployment = await _deployment(db)
    nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="Nifty 50", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    db.add(nifty)
    stocks = {name: await _stock(db, name) for name in ("GAINCO", "LOSECO", "FLATCO")}
    now = datetime.now(timezone.utc)
    for (name, s), rise in zip(stocks.items(), oi_rise):
        for leg in s["legs"]:
            base = 100000.0 if leg.instrument_type == "future" else 10000.0
            db.add(FoOiSnapshot(
                session_date=PREV, mark=MARK_CLOSE, underlying_id=s["eq"].id, instrument_id=leg.id,
                kind="FUT" if leg.instrument_type == "future" else leg.option_type, expiry=leg.expiry, strike=leg.strike,
                oi=base, source="quote", captured_at=now,
            ))
            kite.quotes[f"NFO:{leg.external_ref}"] = {"oi": base * rise, "last_price": 3.2}
    await db.commit()

    for name, (last, prev_close) in {"GAINCO": (104.0, 100.0), "LOSECO": (95.0, 100.0), "FLATCO": (101.0, 100.0)}.items():
        kite.quotes[f"NSE:{name}"] = {"last_price": last, "ohlc": {"close": prev_close}}
    kite.bar("NIFTY 50", _ist(DAY, 9, 15), 25000, 25060, 24990, 25040)
    kite.bar("NIFTY 50", _ist(DAY, 9, 20), 25040, 25080, 24950, 25060 if nifty_green else 24980)
    for i in range(15):
        kite.bar("GAINCO", _ist(PREV, 14, 15) + timedelta(minutes=5 * i), 100, 100.2, 99.8, 100.0)
    kite.bar("GAINCO", _ist(DAY, 9, 15), 100, 104, 100, 103.5)
    kite.bar("GAINCO", _ist(DAY, 9, 20), 103.5, 105, 103, 104.5)
    kite.bar("LOSECO", _ist(DAY, 9, 15), 99, 99.5, 95, 95.5)
    kite.bar("LOSECO", _ist(DAY, 9, 20), 95.5, 96, 94.5, 95)
    return {"portfolio": portfolio, "deployment": deployment, "stocks": stocks}


async def _run(db, world, kite, monkeypatch, state, at: datetime) -> NativeContext:
    monkeypatch.setattr(fo, "_live_broker", lambda ctx: _value(kite))
    ctx = NativeContext(db=db, portfolio=world["portfolio"], deployment=world["deployment"], state=state, now=at)
    await fo.evaluate(ctx)
    await db.commit()
    return ctx


async def _value(v):
    return v


async def _rows(db, scan):
    rows = (await db.execute(select(FoScanResult).where(FoScanResult.scan == scan))).scalars().all()
    return {r.symbol: r for r in rows}


async def test_a_full_day_watchlist_shortlist_breakout_and_sma_exit(db_session: AsyncSession, monkeypatch):
    kite = FakeKite()
    world = await _world(db_session, kite)
    state: dict = {}

    ctx = await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 19, 40))
    assert state["warmed"] and ("instruments", "NSE") in kite.requests
    assert ctx._wake_at == _ist(DAY, 9, 20, 7)

    # 09:20:07 -- watchlist: GAINCO +4% with OI +10%, LOSECO -5% with OI +8%.
    ctx = await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 20, 7))
    assert set(state["setups"]) == {"GAINCO", "LOSECO"}
    gain = state["setups"]["GAINCO"]
    assert (gain["direction"], gain["status"], gain["oi_baseline"]) == ("CE", fo.WATCHLIST, "close")
    assert gain["oi"]["pct_change"] == pytest.approx(10.0)
    assert (gain["oi"]["counted"], gain["oi"]["listed"]) == (17, 17)  # future + 8 CE + 8 PE, no November
    assert state["setups"]["LOSECO"]["direction"] == "PE"
    assert ctx._wake_at == _ist(DAY, 9, 25, 7)
    rows = await _rows(db_session, fo.SCAN_1)
    assert rows["GAINCO"].outcome == fo.WATCHLIST and rows["GAINCO"].oi_change_pct == pytest.approx(10.0)
    assert rows["FLATCO"].outcome == "rejected" and not rows["FLATCO"].passed_move
    titles = (await db_session.execute(select(Alert.title))).scalars().all()
    assert "FLY OI SCN: GAINCO CE on the watchlist" in titles and "FLY OI SCN: 2 on the 9:20 watchlist" in titles

    # 09:25:07 -- Nifty green: both kept; levels are the 09:15-09:25 range.
    ctx = await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 25, 7))
    assert state["nifty"]["bias"] == "green"
    assert (gain["status"], gain["breakout_high"], gain["breakout_low"]) == (fo.WATCHING, 105, 100)
    assert set(await _rows(db_session, fo.SCAN_2)) == {"FLATCO"}  # the late-mover re-scan skips the watchlist
    assert ctx._wake_at == _ist(DAY, 9, 25, 12)  # breakout polls every 5 s

    # 09:26 -- GAINCO breaks 105: buys the strike nearest 2% OTM (105.5 * 1.02 = 107.6 -> 108 CE).
    kite.quotes["NSE:GAINCO"]["last_price"] = 105.5
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 26))
    assert (gain["status"], gain["option_symbol"], gain["entry_premium"]) == (fo.TRIGGERED, "GAINCO26OCT108CE", 3.2)
    assert state["trades_today"] == 1 and state["setups"]["LOSECO"]["status"] == fo.WATCHING
    await db_session.refresh(world["portfolio"])
    assert world["portfolio"].cash == pytest.approx(1_000_000.0 - 3.2 * 500)

    # Candles after the entry: two closes below the SMA -> out after the 9:45 candle closes.
    for i, c in enumerate([105.6, 106.0, 106.5, 95.0, 94.0]):
        kite.bar("GAINCO", _ist(DAY, 9, 25) + timedelta(minutes=5 * i), c, c, c, c)
    kite.quotes["NFO:GAINCO26OCT108CE"]["last_price"] = 1.5
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 45, 7))
    assert gain["status"] == fo.TRIGGERED  # only the 9:40 candle so far
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 50, 7))
    assert (gain["status"], gain["exit_premium"], gain["pnl"]) == (fo.EXITED, 1.5, pytest.approx((1.5 - 3.2) * 500))
    trade = (await db_session.execute(select(PaperNativeTrade))).scalar_one()
    assert trade.pnl == pytest.approx(-850.0) and "8-SMA" in trade.exit_reason
    row = (await _rows(db_session, fo.SCAN_1))["GAINCO"]
    assert (row.outcome, row.option_symbol, row.pnl) == (fo.EXITED, "GAINCO26OCT108CE", pytest.approx(-850.0))

    # 10:30 -- LOSECO never broke 94.5: no trade. 15:10 report.
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 10, 30))
    assert state["setups"]["LOSECO"]["status"] == fo.NO_TRIGGER
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 15, 10))
    assert state["report_sent"]


async def test_red_nifty_drops_gainers(db_session: AsyncSession, monkeypatch):
    kite = FakeKite()
    world = await _world(db_session, kite, nifty_green=False)
    state: dict = {}
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 20, 7))
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 25, 7))
    assert state["nifty"]["bias"] == "red"
    assert state["setups"]["GAINCO"]["status"] == fo.DROPPED_NIFTY
    assert state["setups"]["LOSECO"]["status"] == fo.WATCHING
    row = (await _rows(db_session, fo.SCAN_1))["GAINCO"]
    assert (row.outcome, row.nifty_bias, row.passed_nifty) == (fo.DROPPED_NIFTY, "red", False)


async def test_an_oi_fall_or_a_small_rise_is_rejected(db_session: AsyncSession, monkeypatch):
    kite = FakeKite()
    world = await _world(db_session, kite, oi_rise=(0.90, 1.05, 1.0))
    state: dict = {}
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 20, 7))
    assert state["setups"] == {}
    rows = await _rows(db_session, fo.SCAN_1)
    assert rows["GAINCO"].passed_oi is False and rows["GAINCO"].oi_change_pct == pytest.approx(-10.0)
    assert "needs a rise above 7%" in rows["LOSECO"].reasons[0]


async def test_a_missing_opening_candle_is_retried_until_it_arrives(db_session: AsyncSession, monkeypatch):
    kite = FakeKite()
    world = await _world(db_session, kite)
    late = [b for b in kite.bars[("GAINCO", "5m")] if b["ts"] == _ist(DAY, 9, 15)]
    kite.bars[("GAINCO", "5m")] = [b for b in kite.bars[("GAINCO", "5m")] if b not in late]
    state: dict = {}
    ctx = await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 20, 7))
    assert state["scans"][fo.SCAN_1]["status"] == "running" and "GAINCO" in state["scans"][fo.SCAN_1]["pending"]
    assert "GAINCO" not in state["setups"] and ctx._wake_at == _ist(DAY, 9, 20, 10)
    assert (await _rows(db_session, fo.SCAN_1))["GAINCO"].outcome == "pending"

    kite.bars[("GAINCO", "5m")] += late
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 20, 10))
    assert state["scans"][fo.SCAN_1]["status"] == "done"
    assert state["setups"]["GAINCO"]["status"] == fo.WATCHLIST
    row = (await _rows(db_session, fo.SCAN_1))["GAINCO"]
    assert row.outcome == fo.WATCHLIST and row.retrace_pct == pytest.approx(12.5)


async def test_three_trades_a_day_then_later_breakouts_are_skipped(db_session: AsyncSession, monkeypatch):
    kite = FakeKite()
    portfolio, deployment = await _deployment(db_session)
    names = ["AAA", "BBB", "CCC", "DDD"]
    stocks = {n: await _stock(db_session, n) for n in names}
    await db_session.commit()
    state = fo._fresh_state(DAY)
    state["scans"][fo.SCAN_1]["status"] = state["scans"][fo.SCAN_2]["status"] = "done"
    state["warmed"] = True
    for i, n in enumerate(names):
        state["setups"][n] = {
            "equity_instrument_id": str(stocks[n]["eq"].id), "future_expiry": OCT_EXPIRY.isoformat(), "scan": fo.SCAN_1,
            "direction": "CE", "status": fo.WATCHING, "prev_close": 100.0, "blackout": False,
            "breakout_high": 104.0, "breakout_low": 100.0, "lot_size": None,
        }
        kite.quotes[f"NSE:{n}"] = {"last_price": 105.0 + i}  # DDD moved most, AAA least
        kite.quotes[f"NFO:{n}26OCT108CE"] = {"last_price": 2.0}
        kite.quotes[f"NFO:{n}26OCT110CE"] = {"last_price": 2.0}
    world = {"portfolio": portfolio, "deployment": deployment}
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 30))
    statuses = {n: state["setups"][n]["status"] for n in names}
    assert statuses == {"DDD": fo.TRIGGERED, "CCC": fo.TRIGGERED, "BBB": fo.TRIGGERED, "AAA": fo.LIMIT_REACHED}
    assert state["trades_today"] == 3


async def test_no_baseline_falls_back_to_daily_candles_for_movers_only(db_session: AsyncSession, monkeypatch):
    kite = FakeKite()
    world = await _world(db_session, kite)
    await db_session.execute(FoOiSnapshot.__table__.delete())
    await db_session.commit()
    for s in world["stocks"].values():
        for leg in s["legs"]:
            base = 100000.0 if leg.instrument_type == "future" else 10000.0
            kite.bar(leg.external_ref, _ist(PREV, 0, 0), 1, 1, 1, 1, timeframe="1d", oi=base)
    state: dict = {}
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 20, 7))
    assert state["setups"]["GAINCO"]["oi_baseline"] == "daily_candle"
    assert state["setups"]["GAINCO"]["oi"]["pct_change"] == pytest.approx(10.0)
    daily = [r for r in kite.requests if r[0] == "history" and r[2] == "1d"]
    assert len(daily) == 2 * 17  # the two movers' legs, not FLATCO's
    saved = (await db_session.execute(select(FoOiSnapshot).where(FoOiSnapshot.source == "daily_candle"))).scalars().all()
    assert len(saved) == 34 and {r.session_date for r in saved} == {PREV}


async def test_without_a_zerodha_login_it_waits_and_says_so_once(db_session: AsyncSession, monkeypatch):
    kite = FakeKite()
    world = await _world(db_session, kite)
    monkeypatch.setattr(fo, "_live_broker", lambda ctx: _value(None))
    state: dict = {}
    for at in (_ist(DAY, 9, 20, 7), _ist(DAY, 9, 20, 17)):
        ctx = NativeContext(db=db_session, portfolio=world["portfolio"], deployment=world["deployment"], state=state, now=at)
        await fo.evaluate(ctx)
    await db_session.commit()
    titles = (await db_session.execute(select(Alert.title))).scalars().all()
    assert titles.count("FLY OI SCN: waiting for Zerodha login") == 1
    assert state["scans"][fo.SCAN_1]["status"] == "pending"


async def test_a_scan_that_cannot_run_by_925_skips_the_day(db_session: AsyncSession, monkeypatch):
    kite = FakeKite()
    world = await _world(db_session, kite)
    monkeypatch.setattr(fo, "_live_broker", lambda ctx: _value(None))
    state: dict = {}
    ctx = NativeContext(db=db_session, portfolio=world["portfolio"], deployment=world["deployment"], state=state, now=_ist(DAY, 9, 20, 7))
    await fo.evaluate(ctx)
    # Logged in at 9:40: too late for the 9:20 rules.
    await _run(db_session, world, kite, monkeypatch, state, _ist(DAY, 9, 40))
    assert state["scans"][fo.SCAN_1]["status"] == "done"
    assert state["scans"][fo.SCAN_1]["skipped"] == "Zerodha wasn't logged in by 9:25"
    assert state["setups"] == {} and not [r for r in kite.requests if r[0] == "quote"]
    titles = (await db_session.execute(select(Alert.title))).scalars().all()
    assert titles.count("FLY OI SCN: no scan today") == 1


async def test_telegram_send_failure_never_raises(monkeypatch):
    import httpx
    from types import SimpleNamespace

    from app.services.notifications import telegram

    monkeypatch.setattr(telegram, "get_settings", lambda: SimpleNamespace(telegram_bot_token="t", telegram_chat_id="1"))

    async def _boom(self, *args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx.AsyncClient, "post", _boom)
    await telegram.send_telegram("subject", "body")  # must not raise
