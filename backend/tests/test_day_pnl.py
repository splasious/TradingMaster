"""A stock-holding card's P&L by trading day, after charges
(services/paper_trading/day_pnl.py, 29 Oct): each position from the
previous session's close (or its buy price, when bought that day) to the
day's close (or its sale price; the shown price on the newest day), less
the charges of that day's orders -- a buy's on its day, a sale's on its."""

import uuid
from datetime import date, datetime, time, timezone

import pytest

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading import day_pnl
from app.services.paper_trading.trade_record import charge_breakdown, order_charges

THU, WED, TUE, MON = date(2026, 10, 29), date(2026, 10, 28), date(2026, 10, 27), date(2026, 10, 26)
STOCK = {"exchange": "NSE", "instrument_type": "equity", "side": "long", "quantity": 100}


def at(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=IST)


@pytest.mark.parametrize("opened, closed", [(at(MON, 10, 0), at(THU, 11, 0)), (at(THU, 10, 0), at(THU, 11, 0))])
def test_a_trades_buy_and_sale_charges_add_up_to_its_estimate(opened, closed):
    leg = {**STOCK, "entry_price": 1000.0, "exit_price": 1050.0}
    intraday = opened.date() == closed.date()
    split = order_charges(leg, 1000.0, True, intraday) + order_charges(leg, 1050.0, False, intraday)
    assert split == pytest.approx(charge_breakdown([leg], opened, closed)["total"], abs=0.02)
    assert order_charges({**leg, "exchange": "NFO", "instrument_type": "future", "option_type": None}, 1000.0, True, False) is not None
    assert order_charges({**leg, "exchange": "DELTA", "instrument_type": "crypto"}, 1000.0, True, False) is None


def _position(opened: date, entry: float, closed: date | None = None, exit_price: float | None = None, iid=None):
    return day_pnl._Position(iid or uuid.uuid4(), STOCK, entry, opened, closed, exit_price, closed is None)


def test_each_position_counts_from_the_previous_close_or_its_buy():
    held = _position(MON, 1000.0)  # bought Monday, still held
    bought_today = _position(THU, 500.0)
    sold_today = _position(TUE, 200.0, THU, 210.0)  # bought Tuesday, sold today
    in_and_out = _position(THU, 300.0, THU, 303.0)  # bought and sold today
    sold_wednesday = _position(MON, 400.0, WED, 390.0)  # gone before today
    positions = [held, bought_today, sold_today, in_and_out, sold_wednesday]
    yclose = {held.instrument_id: 1020.0, sold_today.instrument_id: 205.0, sold_wednesday.instrument_id: 395.0}
    now = {held.instrument_id: 1010.0, bought_today.instrument_id: 505.0}

    today = day_pnl.day_figures(positions, THU, yclose, now)
    # held: 100 x (1010 - 1020); bought today: 100 x (505 - 500); sold today: 100 x (210 - 205); in and out: 100 x 3
    assert today.gross == pytest.approx(-1000 + 500 + 500 + 300)
    charges = (order_charges(STOCK, 500.0, True, False) + order_charges(STOCK, 210.0, False, False)
               + order_charges(STOCK, 300.0, True, True) + order_charges(STOCK, 303.0, False, True))
    assert today.charges == pytest.approx(charges, abs=0.01) and today.net == pytest.approx(today.gross - today.charges, abs=0.01)
    assert today.missing == 0 and today.session == THU

    wednesday = day_pnl.day_figures(positions, WED, {held.instrument_id: 990.0, sold_today.instrument_id: 201.0,
                                                     sold_wednesday.instrument_id: 398.0},
                                    {held.instrument_id: 1020.0, sold_today.instrument_id: 205.0})
    # held: 100 x (1020 - 990); sold today, still held then: 100 x (205 - 201); sold Wednesday: 100 x (390 - 398)
    assert wednesday.gross == pytest.approx(3000 + 400 - 800)
    assert wednesday.charges == pytest.approx(order_charges(STOCK, 390.0, False, False), abs=0.01)

    no_close = day_pnl.day_figures([held], THU, {}, now)
    assert (no_close.gross, no_close.missing) == (0.0, 1)


async def _card(db, legs_state: dict, trades: list[dict]):
    user = User(email=f"dp_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="DP")
    db.add(user)
    await db.flush()
    strategy = Strategy(name="MACD - RSI - 15 MIN", owner_id=user.id, code_type="native")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                              python_code="#", position_sizing={}, risk_rules={})
    pool = PaperPortfolio(user_id=user.id, name="DP", cash=1_000_000.0, initial_capital=1_000_000.0)
    db.add_all([version, pool])
    await db.flush()
    deployment = PaperNativeDeployment(portfolio_id=pool.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                       status=DeploymentStatus.ACTIVE.value, state={"holdings": legs_state})
    db.add(deployment)
    await db.flush()
    for t in trades:
        db.add(PaperNativeTrade(deployment_id=deployment.id, exit_reason="ma_exit", pnl_pct=0.0, **t))
    await db.commit()
    return deployment


async def test_the_card_today_and_previous_day_from_the_saved_closes(db_session):
    db = db_session
    a = Instrument(exchange="NSE", symbol="DPA", name="A", instrument_type="equity", data_source="zerodha_kite", external_ref="DPA")
    b = Instrument(exchange="NSE", symbol="DPB", name="B", instrument_type="equity", data_source="zerodha_kite", external_ref="DPB")
    db.add_all([a, b])
    await db.flush()
    for instrument, closes in ((a, {TUE: 990.0, WED: 1020.0}), (b, {TUE: 201.0, WED: 205.0})):
        for d, close in closes.items():
            db.add(OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=datetime.combine(d, time(0), tzinfo=IST).astimezone(timezone.utc),
                               open=close, high=close, low=close, close=close, volume=1, source="test"))
    leg_b = {**STOCK, "instrument_id": str(b.id), "entry_price": 200.0, "exit_price": 210.0}
    deployment = await _card(
        db, {"DPA": {"instrument_id": str(a.id), "quantity": 100, "entry_price": 1000.0, "opened_at": at(MON, 10, 0).isoformat()}},
        [{"opened_at": at(TUE, 10, 0), "closed_at": at(THU, 11, 0), "legs": [leg_b], "pnl": 1000.0, "charges": 50.0}],
    )
    tick_engine.set_real_price(a.id, 1010.0, source="test")
    tick_engine._real_price_at[a.id] = at(THU, 12, 0)
    try:
        pnl = await day_pnl.card_pnl(db, deployment, at(THU, 12, 0).astimezone(timezone.utc))
    finally:
        tick_engine.forget([a.id, b.id])
    assert (pnl.today.session, pnl.previous.session) == (THU, WED)
    assert pnl.today.gross == pytest.approx(100 * (1010 - 1020) + 100 * (210 - 205))
    assert pnl.today.charges == pytest.approx(order_charges(leg_b, 210.0, False, False), abs=0.01)
    assert pnl.previous.gross == pytest.approx(100 * (1020 - 990) + 100 * (205 - 201)) and pnl.previous.charges == 0.0
    assert pnl.realized_charges == 50.0 and pnl.open_charges == pytest.approx(order_charges(STOCK, 1000.0, True, False), abs=0.01)

    saturday = await day_pnl.card_pnl(db, deployment, at(date(2026, 10, 31), 12, 0).astimezone(timezone.utc))
    assert (saturday.today.session, saturday.previous.session) == (date(2026, 10, 30), THU)


async def test_a_card_with_no_stock_holdings_has_none(db_session):
    flat = await _card(db_session, {}, [])
    flat.state = {"position": {"legs": {}}}
    assert await day_pnl.card_pnl(db_session, flat) is None
    future = await _card(db_session, {}, [{"opened_at": at(THU, 10, 0), "closed_at": at(THU, 11, 0), "pnl": 1.0, "charges": 1.0,
                                           "legs": [{**STOCK, "exchange": "NFO", "instrument_type": "future", "instrument_id": str(uuid.uuid4()),
                                                     "entry_price": 1.0, "exit_price": 2.0}]}])
    assert await day_pnl.card_pnl(db_session, future, at(THU, 12, 0).astimezone(timezone.utc)) is None
