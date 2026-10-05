"""While NSE is shut, the Trading page shows open positions at the last
session's close from the saved candles (market_data/closing_price.py),
not the live feed's last tick -- which a restart after 15:30 wipes."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import PaperNativeDeployment
from app.services.backfill_platform.coverage import IST, last_completed_session
from app.services.live_trading import live_runs
from app.services.market_data import closing_price
from app.services.market_data.closing_price import display_prices, session_closes
from app.services.market_data.tick_engine import tick_engine

MON_5_OCT = date(2026, 10, 5)


def _ist(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


async def _stock(db, symbol: str) -> Instrument:
    stock = Instrument(exchange="NSE", symbol=symbol, name=symbol, instrument_type="equity", data_source="zerodha_kite", external_ref=symbol)
    db.add(stock)
    await db.flush()
    return stock


def _candle(instrument: Instrument, timeframe: str, ts: datetime, close: float) -> OhlcvCandle:
    return OhlcvCandle(instrument_id=instrument.id, timeframe=timeframe, ts=ts.astimezone(timezone.utc), open=close, high=close, low=close, close=close,
                       volume=1, source="test")


async def _session(db, stock: Instrument, d: date, last_15m: float, day_close: float | None) -> None:
    """A day's 15-minute candles to 15:15 (the last closing at last_15m) and, once the evening download is in, its day candle."""
    db.add_all([_candle(stock, "15m", _ist(d, 9, 15) + timedelta(minutes=15 * i), last_15m - 24 + i) for i in range(25)])
    if day_close is not None:
        db.add(_candle(stock, "1d", _ist(d, 0), day_close))
    await db.flush()


async def test_after_the_evening_download_it_is_the_day_candle_close(db_session):
    stock = await _stock(db_session, "TATAPOWER")
    await _session(db_session, stock, date(2026, 10, 1), 400.0, 401.0)
    await _session(db_session, stock, MON_5_OCT, 412.0, 412.35)

    for now in (_ist(MON_5_OCT, 19), _ist(date(2026, 10, 6), 8, 30)):  # the same evening; next morning before the open
        close = (await session_closes(db_session, [stock.id], now))[stock.id]
        assert (close.price, close.session, close.provisional) == (412.35, MON_5_OCT, False)
        assert close.as_of == _ist(MON_5_OCT, 15, 30)


async def test_before_it_the_1530_last_price_stands_in_marked_provisional(db_session):
    stock = await _stock(db_session, "BEL")
    await _session(db_session, stock, date(2026, 10, 1), 440.0, 441.0)
    await _session(db_session, stock, MON_5_OCT, 452.5, None)  # 5 Oct's day candle not downloaded yet

    close = (await session_closes(db_session, [stock.id], _ist(MON_5_OCT, 16)))[stock.id]
    assert (close.price, close.session, close.provisional) == (452.5, MON_5_OCT, True)
    assert close.as_of == _ist(MON_5_OCT, 15, 30)  # the 15:15 candle's end


async def test_while_the_market_is_open_it_is_the_live_price(db_session, monkeypatch):
    stock = await _stock(db_session, "HAL")
    nothing_saved = await _stock(db_session, "NEWLISTING")
    await _session(db_session, stock, MON_5_OCT, 4100.0, 4105.0)
    tick_engine.set_real_price(stock.id, 4120.0, "test")
    tick_engine.set_real_price(nothing_saved.id, 99.0, "test")

    assert (await display_prices(db_session, [stock.id], _ist(date(2026, 10, 6), 11))) == {stock.id: (4120.0, None)}
    shut = await display_prices(db_session, [stock.id, nothing_saved.id], _ist(date(2026, 10, 6), 8))
    assert shut[stock.id][0] == 4105.0 and shut[stock.id][1].session == MON_5_OCT
    assert shut[nothing_saved.id] == (99.0, None)  # no candle saved at all: the last live price, as before


async def test_the_trading_page_shows_holdings_and_legs_at_the_close_while_shut(client, seeded_admin, db_session, monkeypatch):
    token = (await client.post("/api/v1/auth/login", json={"email": seeded_admin["email"], "password": seeded_admin["password"]})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    stock = await _stock(db_session, "TATAPOWER")
    last_session = last_completed_session(datetime.now(timezone.utc))  # the endpoint reads the real clock
    await _session(db_session, stock, last_session, 412.0, 412.35)
    await db_session.commit()
    tick_engine.set_real_price(stock.id, 415.0, "test")  # the last tick held in memory

    code = "async def evaluate(ctx):\n    ctx.note('hold', reason='test')\n"
    strategy_id = (await client.post("/api/v1/strategies", json={"name": "RS close test", "version": {"python_code": code, "is_native": True}},
                                     headers=headers)).json()["id"]
    portfolio_id = (await client.get("/api/v1/paper-trading/portfolios", headers=headers)).json()[0]["id"]
    deployment_id = (await client.post("/api/v1/paper-trading/native-deployments", json={"strategy_id": strategy_id, "portfolio_id": portfolio_id},
                                       headers=headers)).json()["id"]
    deployment = (await db_session.execute(select(PaperNativeDeployment))).scalars().all()[-1]
    deployment.state = {"holdings": {"TATAPOWER": {"instrument_id": str(stock.id), "quantity": 100, "entry_price": 400.0, "rank": 1}}}
    await db_session.commit()

    async def listed():
        out = (await client.get("/api/v1/paper-trading/native-deployments", headers=headers)).json()
        return next(d for d in out if d["id"] == deployment_id)["holdings"][0]

    monkeypatch.setattr(closing_price, "nse_market_open", lambda now: False)
    shut = await listed()
    assert shut["current_price"] == 412.35
    assert shut["close"]["session"] == last_session.isoformat() and shut["close"]["provisional"] is False
    monkeypatch.setattr(closing_price, "nse_market_open", lambda now: True)
    live = await listed()
    assert live["current_price"] == 415.0 and live["close"] is None


async def test_the_live_run_card_marks_at_the_close_while_shut(db_session, monkeypatch):
    from app.models.live_native import LiveNativeDeployment, LiveNativePosition
    from app.models.broker import Broker, BrokerAccount
    from app.models.strategy import Strategy, StrategyVersion
    from app.models.user import User

    user = User(email="close@tradingmaster.internal", hashed_password="x", full_name="Close")
    db_session.add(user)
    await db_session.flush()
    broker = (await db_session.execute(select(Broker))).scalars().first() or Broker(code="zerodha_kite", name="Kite", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="Kite", environment="live")
    strategy = Strategy(name="RS live", owner_id=user.id, code_type="native")
    db_session.add_all([account, strategy])
    await db_session.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                              python_code="x", position_sizing={}, risk_rules={})
    db_session.add(version)
    await db_session.flush()
    stock = await _stock(db_session, "BEL")
    await _session(db_session, stock, MON_5_OCT, 452.5, None)
    run = LiveNativeDeployment(owner_id=user.id, strategy_id=strategy.id, strategy_version_id=version.id, broker_account_id=account.id,
                               status="active", lots_per_leg=1, capital=100000, product_style="overnight")
    db_session.add(run)
    await db_session.flush()
    db_session.add(LiveNativePosition(deployment_id=run.id, instrument_id=stock.id, quantity=10, avg_price=450.0, strategy_quantity=10,
                                      product="CNC", opened_at=_ist(MON_5_OCT, 10)))
    await db_session.commit()
    tick_engine.set_real_price(stock.id, 455.0, "test")

    view = await live_runs.run_view(db_session, run, _ist(MON_5_OCT, 17), kill_switch_on=False)
    [leg] = view["positions"]
    assert (leg["current_price"], leg["pnl"], leg["close"]["provisional"]) == (452.5, 25.0, True)
    assert view["unrealised_pnl"] == 25.0 and view["day_pnl"] == 25.0 and view["realised_today"] == 0.0

    view = await live_runs.run_view(db_session, run, _ist(date(2026, 10, 6), 11), kill_switch_on=False)
    assert view["positions"][0]["close"] is None
