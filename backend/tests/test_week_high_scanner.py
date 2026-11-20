"""52 Week High Breakout's "next in line" panel (services/strategy/week_high_scanner.py, 20 Nov):
of its stocks not held, the breakouts -- above the highest weekly high of the 52
completed weeks before this one -- strongest first, then the nearest below; up to 6."""

from datetime import date

import pytest
from fastapi import HTTPException

import app.services.strategy.week_high_scanner as scanner
from app.api.v1.endpoints import paper_native_trading as ep
from app.models.paper_trading import PaperPortfolio
from app.models.strategy import Strategy
from app.models.user import User
from tests.test_week_high_breakout_52 import FRI, STOCKS, Market, at


@pytest.fixture(autouse=True)
def _fresh_caches():
    scanner._LEVELS.clear()
    scanner._QUOTES.clear()
    yield
    scanner._LEVELS.clear()
    scanner._QUOTES.clear()


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await Market(db_session, monkeypatch).build()
    await m.history()  # every week's high 100 -- the 52-week level of every stock
    yield m


def quotes(market, **last: float) -> None:
    market.quotes = {s: (99.0, 100.0, 98.0, last.get(s, 95.0)) for s in STOCKS}


async def test_breakouts_strongest_first_then_the_nearest_below_held_left_out(market):
    a, b, c, d, e = STOCKS[:5]
    quotes(market, **{a: 104.0, b: 102.0, c: 99.5, d: 99.0, e: 101.0})
    panel = await scanner.next_in_line(market.db, {a: {}}, at(FRI, 11, 0))  # a is held
    assert (panel["slots_total"], panel["slots_free"], panel["price_source"], panel["note"]) == (10, 9, "live", None)
    assert panel["next_check"] == "Fri 06-Nov 15:00" and panel["breakouts_now"] == 2
    rows = panel["rows"]
    assert [r["symbol"] for r in rows[:4]] == [b, e, c, d] and len(rows) == scanner.LIMIT == 6
    assert rows[0] == {"symbol": b, "price": 102.0, "level": 100.0, "gap_pct": 2.0, "breakout": True}
    assert (rows[2]["gap_pct"], rows[2]["breakout"]) == (-0.5, False)
    assert all(r["gap_pct"] == -5.0 for r in rows[4:])


async def test_without_a_kite_login_the_last_stored_close_stands_in(market):
    market.logged_in = False
    panel = await scanner.next_in_line(market.db, {}, at(FRI, 11, 0))
    assert panel["price_source"] == "close" and "Zerodha isn't logged in" in panel["note"]
    assert all(r["price"] == 99.0 and r["gap_pct"] == -1.0 for r in panel["rows"])  # the flat days' close, 1% under 100


async def test_quotes_are_kept_a_minute_and_shared(market, monkeypatch):
    calls = []
    real = scanner.whb.kite_quotes

    async def counting(broker, keys):
        calls.append(len(keys))
        return await real(broker, keys)

    monkeypatch.setattr(scanner.whb, "kite_quotes", counting)
    quotes(market)
    await scanner.next_in_line(market.db, {}, at(FRI, 11, 0))
    await scanner.next_in_line(market.db, {}, at(FRI, 11, 0))
    assert calls == [len(STOCKS)]


async def test_the_endpoint_is_its_cards_only(market, db_session):
    quotes(market, **{STOCKS[0]: 103.0})
    owner = await db_session.get(User, (await db_session.get(PaperPortfolio, market.portfolio.id)).user_id)
    panel = await ep.native_deployment_next_in_line(str(market.deployment.id), db=db_session, user=owner)
    assert panel.rows[0].symbol == STOCKS[0] and panel.rows[0].breakout and panel.slots_free == 10
    (await db_session.get(Strategy, market.deployment.strategy_id)).name = "Conti Calendar 15 MIN"
    await db_session.commit()
    with pytest.raises(HTTPException) as err:
        await ep.native_deployment_next_in_line(str(market.deployment.id), db=db_session, user=owner)
    assert err.value.status_code == 404


def test_it_reads_the_strategys_own_rules():
    assert (scanner.whb.BREAKOUT_CANDLES, scanner.whb.MAX_POSITIONS) == (52, 10)
    assert scanner.is_scanned(" 52 Week High Breakout ") and not scanner.is_scanned("52 Candle Breakout 15 MIN")
    assert date(2026, 11, 6).weekday() == 4
