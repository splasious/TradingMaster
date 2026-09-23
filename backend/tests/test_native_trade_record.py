"""Readable closed-trade records for Advanced (native) deployments -- see
app/services/paper_trading/trade_record.py. The fixture trade throughout is
a real one: a NIFTY 23400 short straddle, 650 qty (10 lots of 65), opened
09:45:01 and closed at the 3pm cutoff on 23 Sep 2026."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.instrument import Instrument
from app.models.paper_trading import PaperNativeTrade
from app.models.user import Role, User, UserRole
from app.services.paper_trading.trade_record import estimate_charges, summarize_trade

IST = timezone(timedelta(hours=5, minutes=30))
OPENED = datetime(2026, 9, 23, 9, 45, 1, tzinfo=IST)
CLOSED = datetime(2026, 9, 23, 15, 0, 12, tzinfo=IST)


def _straddle_legs() -> list[dict]:
    common = {"exchange": "NFO", "instrument_type": "option", "strike": 23400.0, "expiry": "2026-09-29", "lot_size": 65, "underlying_symbol": "NIFTY"}
    return [
        {**common, "instrument_id": None, "instrument_symbol": "NIFTY26SEP23400CE", "option_type": "CE",
         "side": "short", "quantity": 650, "entry_price": 122.60, "exit_price": 128.90},
        {**common, "instrument_id": None, "instrument_symbol": "NIFTY26SEP23400PE", "option_type": "PE",
         "side": "short", "quantity": 650, "entry_price": 115.50, "exit_price": 92.85},
    ]


def test_summarize_short_straddle_as_one_readable_row():
    summary = summarize_trade(_straddle_legs())
    assert summary["underlying_symbol"] == "NIFTY"
    assert summary["structure"] == "Short Straddle"
    assert summary["side"] == "short"
    # Combined premium in / out, per unit -- how a straddle is quoted.
    assert summary["entry_price"] == pytest.approx(238.10)
    assert summary["exit_price"] == pytest.approx(221.75)
    assert summary["quantity"] == 650
    assert summary["lots"] == 10
    assert [leg["pnl"] for leg in summary["legs"]] == [pytest.approx(-4095.0), pytest.approx(14722.5)]
    assert all(leg["lots"] == 10 for leg in summary["legs"])


def test_estimate_charges_for_intraday_option_round_trip():
    legs = _straddle_legs()
    # Worked by hand on the NSE options schedule: 4 orders x Rs 20
    # brokerage, 0.1% STT on the two sells, 0.03503% exchange + Rs 10/cr
    # SEBI on all turnover, 0.003% stamp on the two buys, 18% GST.
    sell = 122.60 * 650 + 115.50 * 650
    buy = 128.90 * 650 + 92.85 * 650
    brokerage = 80.0
    exchange = (buy + sell) * 0.0003503
    sebi = (buy + sell) * 0.000001
    expected = brokerage + sell * 0.001 + exchange + sebi + buy * 0.00003 + 0.18 * (brokerage + exchange + sebi)
    assert estimate_charges(legs, OPENED, CLOSED) == pytest.approx(round(expected, 2))
    assert estimate_charges(legs, OPENED, CLOSED) == pytest.approx(377.39, abs=0.01)


def test_estimate_charges_is_none_outside_the_nse_schedule():
    legs = [{"exchange": "DELTA", "instrument_type": "perpetual_future", "option_type": None, "side": "long", "quantity": 1, "entry_price": 60000.0, "exit_price": 61000.0}]
    assert estimate_charges(legs, OPENED, CLOSED) is None
    assert estimate_charges([], OPENED, CLOSED) is None


def test_equity_charges_switch_from_intraday_to_delivery_overnight():
    leg = {"exchange": "NSE", "instrument_type": "equity", "option_type": None, "side": "long", "quantity": 10, "entry_price": 1000.0, "exit_price": 1010.0}
    intraday = estimate_charges([leg], OPENED, CLOSED)
    delivery = estimate_charges([leg], OPENED, CLOSED + timedelta(days=1))
    # Delivery pays 0.1% STT on both sides -- well above intraday's 0.025% on the sell.
    assert delivery > intraday > 0


@pytest.mark.parametrize(
    ("legs", "structure"),
    [
        ([("short", "CE", 23500), ("long", "CE", 23600)], "Bear Call Spread"),
        ([("short", "PE", 23300), ("long", "PE", 23200)], "Bull Put Spread"),
        ([("long", "CE", 23400), ("short", "CE", 23500)], "Bull Call Spread"),
        ([("short", "CE", 23500), ("short", "PE", 23300)], "Short Strangle"),
        ([("short", "CE", 23500), ("long", "CE", 23600), ("short", "PE", 23300), ("long", "PE", 23200)], "Iron Condor"),
        ([("long", "PE", 23400)], "Long PE"),
    ],
)
def test_structure_names(legs, structure):
    # Premiums fall with distance from 23400, so each spread nets the right way round.
    raw = [
        {"side": side, "option_type": opt, "strike": strike, "quantity": 65, "lot_size": 65,
         "entry_price": 200.0 - abs(strike - 23400) / 2, "exit_price": 150.0}
        for side, opt, strike in legs
    ]
    assert summarize_trade(raw)["structure"] == structure


def test_sell_buy_side_spelling_reads_as_short_long():
    legs = [{**leg, "side": "sell"} for leg in _straddle_legs()]
    summary = summarize_trade(legs)
    assert summary["structure"] == "Short Straddle"
    assert [leg["side"] for leg in summary["legs"]] == ["short", "short"]
    assert [leg["pnl"] for leg in summary["legs"]] == [pytest.approx(-4095.0), pytest.approx(14722.5)]
    assert estimate_charges(legs, OPENED, CLOSED) == estimate_charges(_straddle_legs(), OPENED, CLOSED)


def test_unequal_leg_quantities_leave_trade_level_price_blank():
    legs = _straddle_legs()
    legs[1]["quantity"] = 325
    summary = summarize_trade(legs)
    assert summary["quantity"] is None
    assert summary["entry_price"] is None and summary["exit_price"] is None
    assert summary["lots"] is None


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _seed_straddle_instruments(db_session: AsyncSession) -> tuple[Instrument, Instrument]:
    index = Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    db_session.add(index)
    await db_session.flush()
    options = [
        Instrument(
            exchange="NFO", symbol=f"NIFTY26SEP23400{opt}", name=f"NIFTY26SEP23400{opt}", instrument_type="option",
            data_source="zerodha_kite", external_ref=f"NIFTY26SEP23400{opt}", expiry=date(2026, 9, 29), strike=23400.0,
            option_type=opt, lot_size=65, underlying_instrument_id=index.id,
        )
        for opt in ("CE", "PE")
    ]
    db_session.add_all(options)
    await db_session.commit()
    return options[0], options[1]


async def _deploy_recording_strategy(client: AsyncClient, headers: dict, legs_literal: str, exit_reason: str) -> str:
    code = (
        "from datetime import datetime\n"
        "async def evaluate(ctx):\n"
        "    await ctx.record_trade(\n"
        f"        legs={legs_literal}, pnl=10627.5, pnl_pct=2.13, exit_reason={exit_reason!r},\n"
        f"        opened_at=datetime.fromisoformat({OPENED.isoformat()!r}), closed_at=datetime.fromisoformat({CLOSED.isoformat()!r}),\n"
        "    )\n"
        "    ctx.note('exited', signal='COVER')\n"
    )
    strategy_resp = await client.post(
        "/api/v1/strategies", json={"name": "AM OP TRD 15 MIN", "version": {"python_code": code, "is_native": True}}, headers=headers,
    )
    assert strategy_resp.status_code == 201, strategy_resp.text
    portfolio_id = (await client.get("/api/v1/paper-trading/portfolios", headers=headers)).json()[0]["id"]
    deploy_resp = await client.post(
        "/api/v1/paper-trading/native-deployments",
        json={"strategy_id": strategy_resp.json()["id"], "portfolio_id": portfolio_id}, headers=headers,
    )
    return deploy_resp.json()["id"]


async def test_record_trade_saves_a_readable_record(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    ce, pe = await _seed_straddle_instruments(db_session)
    headers = {"Authorization": f"Bearer {await _login(client, seeded_admin['email'], seeded_admin['password'])}"}
    legs_literal = repr([
        {"instrument_id": str(ce.id), "side": "short", "quantity": 650, "entry_price": 122.60, "exit_price": 128.90},
        {"instrument_id": str(pe.id), "side": "short", "quantity": 650, "entry_price": 115.50, "exit_price": 92.85},
    ])
    # Longer than the old 30-char column -- used to fail the insert on
    # Postgres and lose the whole close.
    long_reason = "time_cutoff_3pm_" + "x" * 150
    deployment_id = await _deploy_recording_strategy(client, headers, legs_literal, long_reason)

    await client.post(f"/api/v1/paper-trading/native-deployments/{deployment_id}/evaluate", headers=headers)

    # What's saved stands on its own: contract details and charges on the row itself.
    saved = (await db_session.execute(select(PaperNativeTrade))).scalar_one()
    await db_session.refresh(saved)
    assert saved.charges == pytest.approx(377.39, abs=0.01)
    assert len(saved.exit_reason) == 100
    assert saved.legs[0]["instrument_symbol"] == "NIFTY26SEP23400CE"
    assert saved.legs[0]["strike"] == 23400.0 and saved.legs[0]["option_type"] == "CE"
    assert saved.legs[0]["expiry"] == "2026-09-29" and saved.legs[0]["lot_size"] == 65
    assert saved.legs[0]["underlying_symbol"] == "NIFTY"

    trade = (await client.get(f"/api/v1/paper-trading/native-trades?deployment_id={deployment_id}", headers=headers)).json()[0]
    assert trade["strategy_name"] == "AM OP TRD 15 MIN"
    assert trade["currency"] == "INR"
    # Same instant back, with an explicit offset -- SQLite used to hand back
    # the IST wall clock with no offset, which a browser reads as local time.
    assert datetime.fromisoformat(trade["opened_at"]) == OPENED
    assert datetime.fromisoformat(trade["closed_at"]) == CLOSED
    assert trade["underlying_symbol"] == "NIFTY"
    assert trade["structure"] == "Short Straddle"
    assert trade["entry_price"] == pytest.approx(238.10)
    assert trade["exit_price"] == pytest.approx(221.75)
    assert trade["quantity"] == 650 and trade["lots"] == 10
    assert trade["pnl"] == 10627.5
    assert trade["charges"] == pytest.approx(377.39, abs=0.01)
    assert trade["net_pnl"] == pytest.approx(10627.5 - 377.39, abs=0.01)
    assert [leg["pnl"] for leg in trade["legs"]] == [pytest.approx(-4095.0), pytest.approx(14722.5)]

    all_trades = (await client.get("/api/v1/paper-trading/native-trades", headers=headers)).json()
    assert all_trades[0]["structure"] == "Short Straddle"


async def test_trades_saved_before_the_snapshot_are_resolved_on_read(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    """A row saved by the old record_trade(): bare instrument ids, no charges."""
    ce, pe = await _seed_straddle_instruments(db_session)
    headers = {"Authorization": f"Bearer {await _login(client, seeded_admin['email'], seeded_admin['password'])}"}
    deployment_id = await _deploy_recording_strategy(client, headers, "[]", "manual")

    db_session.add(PaperNativeTrade(
        deployment_id=uuid.UUID(deployment_id), opened_at=OPENED, closed_at=CLOSED, pnl=10627.5, pnl_pct=2.13, exit_reason="time_cutoff_3pm",
        legs=[
            {"instrument_id": str(ce.id), "side": "short", "quantity": 650, "entry_price": 122.60, "exit_price": 128.90},
            {"instrument_id": str(pe.id), "side": "short", "quantity": 650, "entry_price": 115.50, "exit_price": 92.85},
        ],
    ))
    await db_session.commit()

    trade = (await client.get(f"/api/v1/paper-trading/native-trades?deployment_id={deployment_id}", headers=headers)).json()[0]
    assert trade["legs"][1]["instrument_symbol"] == "NIFTY26SEP23400PE"
    assert trade["structure"] == "Short Straddle"
    assert trade["lots"] == 10
    assert trade["charges"] == pytest.approx(377.39, abs=0.01)


async def test_other_users_cannot_read_a_deployments_trades(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    admin_headers = {"Authorization": f"Bearer {await _login(client, seeded_admin['email'], seeded_admin['password'])}"}
    deployment_id = await _deploy_recording_strategy(client, admin_headers, "[]", "manual")
    await client.post(f"/api/v1/paper-trading/native-deployments/{deployment_id}/evaluate", headers=admin_headers)

    trader_role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    other = User(email="nt-other@tradingmaster.internal", hashed_password=hash_password("OtherPass123!"), full_name="Other")
    other.user_roles = [UserRole(role=trader_role)]
    db_session.add(other)
    await db_session.commit()
    other_headers = {"Authorization": f"Bearer {await _login(client, 'nt-other@tradingmaster.internal', 'OtherPass123!')}"}

    resp = await client.get(f"/api/v1/paper-trading/native-trades?deployment_id={deployment_id}", headers=other_headers)
    assert resp.status_code == 403
