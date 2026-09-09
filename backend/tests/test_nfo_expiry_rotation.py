import json
import uuid
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_payload
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.models.instrument import Instrument
from app.services.backfill_platform import nfo_expiry_rotation as rotation
from app.services.broker.zerodha_broker import ZerodhaKiteBroker


class db_session_cm:
    """Redirects the module's own `async with AsyncSessionLocal()` to the
    test's isolated db_session -- same pattern as test_kite_ticker_service.py."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc) -> None:
        return None


def _nfo_row(name: str, expiry: str, strike: float, option_type: str, token: int) -> dict:
    return {
        "name": name, "expiry": expiry, "strike": str(strike),
        "instrument_type": option_type, "tradingsymbol": f"{name}{expiry.replace('-', '')}{strike:g}{option_type}",
        "instrument_token": str(token), "lot_size": "50",
    }


def _option_rows(name: str, expiry: str, strikes: list[float], start_token: int) -> list[dict]:
    rows = []
    token = start_token
    for strike in strikes:
        rows.append(_nfo_row(name, expiry, strike, "CE", token))
        token += 1
        rows.append(_nfo_row(name, expiry, strike, "PE", token))
        token += 1
    return rows


def test_select_strike_window_centers_on_closest_available_strike():
    strikes = [100.0, 150.0, 200.0, 250.0, 300.0]
    assert rotation._select_strike_window(strikes, spot=205.0, window=1) == [150.0, 200.0, 250.0]


def test_select_strike_window_clamps_at_the_edge():
    strikes = [100.0, 150.0, 200.0]
    assert rotation._select_strike_window(strikes, spot=100.0, window=1) == [100.0, 150.0]


def test_select_strike_window_empty_when_no_strikes():
    assert rotation._select_strike_window([], spot=100.0, window=5) == []


def test_target_expiries_filters_sorts_and_caps():
    rows = [
        *_option_rows("NIFTY", "2026-09-02", [23000], 1),
        *_option_rows("NIFTY", "2026-09-09", [23000], 10),
        *_option_rows("NIFTY", "2026-08-01", [23000], 20),  # already past
        *_option_rows("BANKNIFTY", "2026-09-02", [50000], 30),  # different underlying
    ]
    result = rotation._target_expiries(rows, "NIFTY", today=date(2026, 9, 1), count=4)
    assert result == [date(2026, 9, 2), date(2026, 9, 9)]


def test_target_expiries_caps_at_count():
    rows = []
    for i, day in enumerate(["09", "16", "23", "30"], start=1):
        rows += _option_rows("NIFTY", f"2026-09-{day}", [23000], i * 100)
    rows += _option_rows("NIFTY", "2026-10-07", [23000], 900)
    result = rotation._target_expiries(rows, "NIFTY", today=date(2026, 9, 1), count=4)
    assert len(result) == 4
    assert date(2026, 10, 7) not in result


def test_kite_name_for_uses_alias_table_with_fallback():
    assert rotation._kite_name_for("NIFTY 50") == "NIFTY"
    assert rotation._kite_name_for("NIFTY BANK") == "BANKNIFTY"
    assert rotation._kite_name_for("RELIANCE") == "RELIANCE"


async def _seed_underlying(db_session: AsyncSession) -> Instrument:
    underlying = Instrument(
        exchange="NSE", symbol="NIFTY 50", name="Nifty 50 Index", instrument_type="index",
        data_source="zerodha_kite", external_ref="NIFTY 50",
    )
    db_session.add(underlying)
    await db_session.commit()
    return underlying


FUTURE_EXPIRY = date.today() + timedelta(days=7)
FUTURE_EXPIRY_STR = FUTURE_EXPIRY.isoformat()


async def test_ensure_underlying_expiries_backfills_missing_expiry_with_atm_window(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(rotation, "STRIKE_WINDOW", 1)
    underlying = await _seed_underlying(db_session)

    rows = _option_rows("NIFTY", FUTURE_EXPIRY_STR, [22900, 22950, 23000, 23050, 23100], start_token=1)

    async def fake_get_instruments(self, segment="NSE"):
        assert segment == "NFO"
        return rows

    async def fake_get_ltp(self, exchange, tradingsymbol):
        assert exchange == "NSE" and tradingsymbol == "NIFTY 50"
        return {"price": 23010.0, "instrument_token": 999}

    queued_job_ids: list[uuid.UUID] = []

    async def fake_run_job(job_id):
        queued_job_ids.append(job_id)

    monkeypatch.setattr(ZerodhaKiteBroker, "get_instruments", fake_get_instruments)
    monkeypatch.setattr(ZerodhaKiteBroker, "get_ltp", fake_get_ltp)
    monkeypatch.setattr(rotation, "run_bf_backfill_job", fake_run_job)

    broker = ZerodhaKiteBroker()
    added = await rotation._ensure_underlying_expiries(db_session, broker, underlying, account_user_id=uuid.uuid4())

    # ATM (23010 -> closest strike 23000) ± 1 -> {22950, 23000, 23050} x {CE, PE} = 6 legs.
    assert added == 6
    assert len(queued_job_ids) == 6

    from app.models.backfill_platform import BfSymbol
    from sqlalchemy import select
    symbols = (await db_session.execute(select(BfSymbol).where(BfSymbol.source == "zerodha_nfo"))).scalars().all()
    assert {s.strike for s in symbols} == {22950.0, 23000.0, 23050.0}
    assert {s.option_type for s in symbols} == {"CE", "PE"}
    assert all(s.expiry == FUTURE_EXPIRY for s in symbols)


async def test_ensure_underlying_expiries_skips_expiry_already_in_catalog(db_session: AsyncSession, monkeypatch):
    underlying = await _seed_underlying(db_session)
    # Simulate catalog_sync_scheduler having already bridged this expiry
    # into the main Instrument catalog on a previous tick.
    db_session.add(Instrument(
        exchange="NFO", symbol="NIFTY_ALREADY_SYNCED_CE", name="NIFTY_ALREADY_SYNCED_CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY_ALREADY_SYNCED_CE", expiry=FUTURE_EXPIRY, strike=23000,
        option_type="CE", underlying_instrument_id=underlying.id,
    ))
    await db_session.commit()

    rows = _option_rows("NIFTY", FUTURE_EXPIRY_STR, [23000], start_token=1)

    async def fake_get_instruments(self, segment="NSE"):
        return rows

    async def fake_get_ltp(self, exchange, tradingsymbol):
        raise AssertionError("get_ltp should not be called when the expiry is already covered")

    monkeypatch.setattr(ZerodhaKiteBroker, "get_instruments", fake_get_instruments)
    monkeypatch.setattr(ZerodhaKiteBroker, "get_ltp", fake_get_ltp)

    broker = ZerodhaKiteBroker()
    added = await rotation._ensure_underlying_expiries(db_session, broker, underlying, account_user_id=uuid.uuid4())
    assert added == 0


async def _seed_connected_account(db_session: AsyncSession) -> uuid.UUID:
    from app.models.user import Role, User, UserRole

    role = Role(id=uuid.uuid4(), name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"rotation_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Rotation Test")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()
    broker_row = Broker(code="zerodha_kite", name="Zerodha Kite", is_enabled=True)
    db_session.add(broker_row)
    await db_session.flush()
    account = BrokerAccount(user_id=user.id, broker_id=broker_row.id, account_label="Rotation Test", environment="paper")
    db_session.add(account)
    await db_session.flush()
    creds = {"api_key": "kitekey", "api_secret": "kitesecret", "access_token": "tok"}
    db_session.add(BrokerCredential(broker_account_id=account.id, encrypted_payload=encrypt_payload(json.dumps(creds))))
    db_session.add(BrokerConnection(broker_account_id=account.id, status=ConnectionStatus.CONNECTED.value))
    await db_session.commit()
    return user.id


async def test_check_once_no_error_when_nothing_connected(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(rotation, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    scheduler = rotation.NfoExpiryRotationScheduler()
    added = await scheduler.check_once()
    assert added == 0
    assert scheduler.last_error == "No connected Zerodha account"


async def test_check_once_backfills_every_tracked_underlying(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(rotation, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    monkeypatch.setattr(rotation, "STRIKE_WINDOW", 0)
    user_id = await _seed_connected_account(db_session)
    underlying = await _seed_underlying(db_session)
    # A second NFO instrument (an already-synced option, at a DIFFERENT
    # expiry than the one below) is what makes this underlying show up as
    # "tracked" at all -- _tracked_underlyings derives from real NFO rows,
    # same as the /options/underlyings endpoint.
    older_expiry = FUTURE_EXPIRY - timedelta(days=7)
    db_session.add(Instrument(
        exchange="NFO", symbol="NIFTY_OLDER_EXPIRY_CE", name="NIFTY_OLDER_EXPIRY_CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY_OLDER_EXPIRY_CE", expiry=older_expiry, strike=23000,
        option_type="CE", underlying_instrument_id=underlying.id,
    ))
    await db_session.commit()

    rows = _option_rows("NIFTY", FUTURE_EXPIRY_STR, [23000], start_token=1)

    async def fake_get_instruments(self, segment="NSE"):
        return rows

    async def fake_get_ltp(self, exchange, tradingsymbol):
        return {"price": 23000.0, "instrument_token": 999}

    queued: list[uuid.UUID] = []

    async def fake_run_job(job_id):
        queued.append(job_id)

    monkeypatch.setattr(ZerodhaKiteBroker, "get_instruments", fake_get_instruments)
    monkeypatch.setattr(ZerodhaKiteBroker, "get_ltp", fake_get_ltp)
    monkeypatch.setattr(rotation, "run_bf_backfill_job", fake_run_job)

    scheduler = rotation.NfoExpiryRotationScheduler()
    added = await scheduler.check_once()
    assert added == 2  # ATM only (window=0) x {CE, PE}
    assert len(queued) == 2
    assert scheduler.last_error is None
