"""The PCR Analysis tab's live NIFTY 50 chart (services/options/oi_chart.py,
3 Nov): NIFTY's 15-minute candles and the one forming, the latest record,
and the expiry's contracts with their OI at it, the record before and the
previous close, each with the instrument the page reads live OI from."""

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.pcr import PcrStrikeOi
from app.services.market_data.tick_engine import tick_engine
from app.services.options import oi_chart
from tests.test_oi_levels import EXPIRIES, LAKH, NEAR, _nifty_candles, _three_records
from tests.test_pcr_snapshots import at


async def test_the_chart_starts_from_the_candles_the_record_and_its_contracts(db_session: AsyncSession, client, seeded_admin):
    db = db_session
    snap = await _three_records(db)  # 24 Sep 15:30, 25 Sep 10:00 and 10:15; NIFTY 22,560.5
    nifty = await _nifty_candles(db, at(25, 10, 0))
    symbols = dict(
        (await db.execute(
            select(PcrStrikeOi.strike, PcrStrikeOi.tradingsymbol)
            .where(PcrStrikeOi.snapshot_id == snap.id, PcrStrikeOi.expiry == NEAR, PcrStrikeOi.option_type == "CE")
        )).all()
    )
    contract = Instrument(exchange="NFO", symbol=symbols[22600.0], name=symbols[22600.0], instrument_type="option",
                          data_source="zerodha_kite", external_ref=symbols[22600.0], expiry=NEAR, strike=22600.0, option_type="CE")
    db.add(contract)
    await db.commit()

    now = at(25, 10, 20)
    tick_engine.set_real_price(nifty.id, 22570.0, source="test")
    tick_engine._real_price_at[nifty.id] = now
    try:
        chart = await oi_chart.oi_chart(db, "NIFTY", now=now)
    finally:
        tick_engine.forget([nifty.id])

    assert chart["nifty_instrument_id"] == str(nifty.id) and chart["market_open"] and chart["live_price"] == 22570.0
    assert len(chart["candles"]) == 13 and chart["candles"][-1]["ts"] == at(25, 10, 0)
    forming = chart["forming"]
    assert forming["ts"] == at(25, 10, 15) and forming["open"] == chart["candles"][-1]["close"] and forming["close"] == 22570.0
    assert (chart["record_ts"], chart["record_spot"], chart["expiry"]) == (at(25, 10, 15), 22560.5, NEAR)
    assert chart["expiries"] == EXPIRIES[:len(chart["expiries"])] and chart["prev_ts"] == at(25, 10, 0)

    by_key = {(c["strike"], c["option_type"]): c for c in chart["contracts"]}
    call = by_key[(22600.0, "CE")]
    assert (call["oi"], call["prev_oi"], call["day_oi"]) == (98.4 * LAKH, 86.3 * LAKH, 58.2 * LAKH)
    assert call["instrument_id"] == str(contract.id)
    assert by_key[(22500.0, "PE")]["instrument_id"] is None  # no instrument for it: record OI only
    assert {c["option_type"] for c in chart["contracts"]} == {"CE", "PE"}

    # Another of the record's expiries; one it didn't capture falls back to the nearest.
    other = await oi_chart.oi_chart(db, "NIFTY", EXPIRIES[1], now=now)
    assert other["expiry"] == EXPIRIES[1] and all(c["oi"] == 10.0 * LAKH for c in other["contracts"])
    assert (await oi_chart.oi_chart(db, "NIFTY", date(2030, 1, 1), now=now))["expiry"] == NEAR

    # After the close: the last session's record and candles, nothing forming.
    evening = await oi_chart.oi_chart(db, "NIFTY", now=at(25, 18, 0))
    assert not evening["market_open"] and evening["forming"] is None and evening["record_ts"] == at(25, 10, 15)

    login = await client.post("/api/v1/auth/login", json={"email": seeded_admin["email"], "password": seeded_admin["password"]})
    auth_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = await client.get("/api/v1/options/oi-chart?underlying=NIFTY", headers=auth_headers)
    assert response.status_code == 200 and response.json()["underlying"] == "NIFTY"
    assert (await client.get("/api/v1/options/oi-chart?underlying=BANKNIFTY", headers=auth_headers)).status_code == 404


async def test_with_no_records_the_chart_has_candles_alone(db_session: AsyncSession):
    nifty = await _nifty_candles(db_session, at(25, 10, 0))
    chart = await oi_chart.oi_chart(db_session, "NIFTY", now=at(25, 10, 20) + timedelta(hours=8))
    assert chart["nifty_instrument_id"] == str(nifty.id) and chart["candles"]
    assert chart["record_ts"] is None and chart["contracts"] == [] and chart["expiry"] is None
