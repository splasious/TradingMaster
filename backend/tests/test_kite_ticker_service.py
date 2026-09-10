import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_payload
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.models.instrument import Instrument
from app.services.broker import kite_ticker_service as svc
from app.services.broker.zerodha_broker import ZerodhaKiteBroker
from app.services.market_data.tick_engine import TickEngine


class db_session_cm:
    """kite_ticker_service opens its own `async with AsyncSessionLocal()`
    internally (same as real_price_feed.py) -- this redirects that to the
    test's isolated db_session instead of the app's real configured DB,
    matching tests/test_real_price_feed.py's identical pattern."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc) -> None:
        return None


def test_tick_engine_oi_channel_has_no_simulated_fallback():
    engine = TickEngine()
    iid = uuid.uuid4()
    engine.subscribe(iid, seed_price=100.0)

    assert engine.get_current_oi(iid) is None
    msg = engine._next_tick_message(iid, "t1")
    assert msg["open_interest"] is None

    engine.set_real_oi(iid, 12345.0)
    assert engine.get_current_oi(iid) == 12345.0
    msg2 = engine._next_tick_message(iid, "t2")
    assert msg2["open_interest"] == 12345.0


async def _seed_connected_account(db_session: AsyncSession, *, connected: bool = True, access_token: str | None = "tok_xyz") -> None:
    role_id = uuid.uuid4()
    from app.models.user import Role, User, UserRole

    role = Role(id=role_id, name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"kite_ticker_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Kite Ticker Test")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    broker = Broker(code="zerodha_kite", name="Zerodha Kite", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="Kite Ticker Test", environment="paper")
    db_session.add(account)
    await db_session.flush()
    creds = {"api_key": "kitekey", "api_secret": "kitesecret"}
    if access_token is not None:
        creds["access_token"] = access_token
    db_session.add(BrokerCredential(broker_account_id=account.id, encrypted_payload=encrypt_payload(json.dumps(creds))))
    db_session.add(BrokerConnection(
        broker_account_id=account.id,
        status=ConnectionStatus.CONNECTED.value if connected else ConnectionStatus.ERROR.value,
    ))
    await db_session.commit()


async def test_find_connected_credentials_returns_none_without_a_connected_account(db_session: AsyncSession):
    assert await svc.find_connected_zerodha_credentials(db_session) is None


async def test_find_connected_credentials_ignores_disconnected_account(db_session: AsyncSession):
    await _seed_connected_account(db_session, connected=False)
    assert await svc.find_connected_zerodha_credentials(db_session) is None


async def test_find_connected_credentials_returns_decrypted_creds(db_session: AsyncSession):
    await _seed_connected_account(db_session, connected=True, access_token="real_token")
    creds = await svc.find_connected_zerodha_credentials(db_session)
    assert creds is not None
    assert creds["api_key"] == "kitekey"
    assert creds["access_token"] == "real_token"


async def test_find_connected_credentials_none_when_no_access_token_yet(db_session: AsyncSession):
    """A connected-but-not-logged-in-yet Kite account (api_key/secret
    stored, interactive login not completed) has no access_token -- must
    not be treated as ready to stream."""
    await _seed_connected_account(db_session, connected=True, access_token=None)
    assert await svc.find_connected_zerodha_credentials(db_session) is None


async def test_resolve_kite_token_map_matches_by_tradingsymbol_per_segment(db_session: AsyncSession, monkeypatch):
    inst = Instrument(
        exchange="NFO", symbol="NIFTY26SEP23000CE", name="NIFTY26SEP23000CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY26SEP23000CE",
    )
    equity = Instrument(exchange="NSE", symbol="INFY", name="Infosys", instrument_type="equity", data_source="zerodha_kite", external_ref="INFY")
    stale = Instrument(exchange="NSE", symbol="TCS", name="TCS", instrument_type="equity", data_source="yahoo_nse", external_ref="TCS.NS")
    db_session.add_all([inst, equity, stale])
    await db_session.commit()

    async def fake_get_instruments(self, segment="NSE"):
        if segment == "NFO":
            return [
                {"tradingsymbol": "NIFTY26SEP23000CE", "instrument_token": "111"},
                {"tradingsymbol": "SOMETHING_ELSE", "instrument_token": "222"},
            ]
        assert segment == "NSE"
        return [{"tradingsymbol": "INFY", "instrument_token": "333"}]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_instruments", fake_get_instruments)

    token_maps = await svc._resolve_kite_token_map(db_session, "kitekey")
    assert token_maps == {"NFO": {111: inst.id}, "NSE": {333: equity.id}}  # unmatched NFO row and stale-source NSE row both ignored


async def test_resolve_kite_token_map_empty_when_no_instruments(db_session: AsyncSession):
    assert await svc._resolve_kite_token_map(db_session, "kitekey") == {}


async def test_resolve_kite_token_map_falls_back_to_be_suffix(db_session: AsyncSession, monkeypatch):
    """A stock NSE moved to its "BE" surveillance series (e.g. HEG, HFCL --
    see zerodha_broker.py) must still resolve to a live-tick token, not
    silently drop out of the subscription map."""
    heg = Instrument(exchange="NSE", symbol="HEG", name="HEG", instrument_type="equity", data_source="zerodha_kite", external_ref="HEG")
    db_session.add(heg)
    await db_session.commit()

    async def fake_get_instruments(self, segment="NSE"):
        assert segment == "NSE"
        return [{"tradingsymbol": "HEG-BE", "instrument_token": "444"}]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_instruments", fake_get_instruments)

    token_maps = await svc._resolve_kite_token_map(db_session, "kitekey")
    assert token_maps == {"NSE": {444: heg.id}}


def test_on_ticks_updates_price_and_oi_for_mapped_instruments():
    engine = TickEngine()
    service = svc.KiteTickerService(engine)
    iid = uuid.uuid4()
    service._token_map = {738561: iid}

    service._on_ticks(None, [{"instrument_token": 738561, "last_price": 4084.0, "oi": 21845}])

    assert engine.get_current_price(iid) == 4084.0
    assert engine.get_current_oi(iid) == 21845


def test_on_ticks_ignores_unmapped_instrument_tokens():
    engine = TickEngine()
    service = svc.KiteTickerService(engine)
    service._token_map = {}

    # Must not raise, must not touch the engine for a token it doesn't know.
    service._on_ticks(None, [{"instrument_token": 999999, "last_price": 100.0, "oi": 5}])

    assert engine.get_current_price(uuid.uuid4()) is None


def test_on_ticks_skips_oi_when_absent_ltp_mode():
    """LTP-mode ticks (no OI field at all) must not clobber a previously
    known OI value with None."""
    engine = TickEngine()
    service = svc.KiteTickerService(engine)
    iid = uuid.uuid4()
    service._token_map = {1: iid}
    engine.set_real_oi(iid, 500.0)

    service._on_ticks(None, [{"instrument_token": 1, "last_price": 10.0}])  # no "oi" key

    assert engine.get_current_oi(iid) == 500.0  # untouched
    assert engine.get_current_price(iid) == 10.0


class _FakeTicker:
    """Stands in for kiteconnect.KiteTicker -- records what the service
    does with it without touching Twisted, a thread, or the network."""

    MODE_FULL = "full"
    MODE_LTP = "ltp"
    instances: list["_FakeTicker"] = []

    def __init__(self, api_key, access_token):
        self.api_key = api_key
        self.access_token = access_token
        self.on_ticks = None
        self.on_connect = None
        self.on_close = None
        self.on_error = None
        self.connected = False
        self.closed = False
        self.subscribed = None
        self.mode_calls: list[tuple[str, list[int]]] = []
        _FakeTicker.instances.append(self)

    def connect(self, threaded=False):
        self.connected = True
        if self.on_connect:
            self.on_connect(self, {})

    def subscribe(self, tokens):
        self.subscribed = list(tokens)

    def set_mode(self, mode, tokens):
        self.mode_calls.append((mode, list(tokens)))

    def close(self):
        self.closed = True


async def test_refresh_builds_ticker_and_subscribes_resolved_tokens(db_session: AsyncSession, monkeypatch):
    _FakeTicker.instances.clear()
    monkeypatch.setattr(svc, "KiteTicker", _FakeTicker)
    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    await _seed_connected_account(db_session, connected=True, access_token="tok_a")
    inst = Instrument(
        exchange="NFO", symbol="NIFTY26SEP23000CE", name="NIFTY26SEP23000CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY26SEP23000CE",
    )
    db_session.add(inst)
    await db_session.commit()

    async def fake_get_instruments(self, segment="NSE"):
        return [{"tradingsymbol": "NIFTY26SEP23000CE", "instrument_token": "555"}]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_instruments", fake_get_instruments)

    engine = TickEngine()
    service = svc.KiteTickerService(engine)
    await service._refresh()

    assert len(_FakeTicker.instances) == 1
    ticker = _FakeTicker.instances[0]
    assert ticker.access_token == "tok_a"
    assert ticker.connected is True
    assert ticker.subscribed == [555]
    assert ticker.mode_calls == [("full", [555])]
    assert service.last_connected_at is not None

    # A live tick now updates the engine through the real _on_ticks path.
    ticker.on_ticks(ticker, [{"instrument_token": 555, "last_price": 120.5, "oi": 900}])
    assert engine.get_current_price(inst.id) == 120.5
    assert engine.get_current_oi(inst.id) == 900


async def test_refresh_skips_rebuild_when_token_unchanged(db_session: AsyncSession, monkeypatch):
    _FakeTicker.instances.clear()
    monkeypatch.setattr(svc, "KiteTicker", _FakeTicker)
    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    await _seed_connected_account(db_session, connected=True, access_token="tok_stable")
    inst = Instrument(
        exchange="NFO", symbol="NIFTY26SEP23000CE", name="NIFTY26SEP23000CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY26SEP23000CE",
    )
    db_session.add(inst)
    await db_session.commit()

    async def fake_get_instruments(self, segment="NSE"):
        return [{"tradingsymbol": "NIFTY26SEP23000CE", "instrument_token": "555"}]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_instruments", fake_get_instruments)

    engine = TickEngine()
    service = svc.KiteTickerService(engine)
    await service._refresh()
    assert len(_FakeTicker.instances) == 1

    await service._refresh()  # same token still connected -> no new ticker, no old one closed
    assert len(_FakeTicker.instances) == 1
    assert _FakeTicker.instances[0].closed is False


async def test_refresh_rebuilds_and_closes_old_ticker_on_new_token(db_session: AsyncSession, monkeypatch):
    _FakeTicker.instances.clear()
    monkeypatch.setattr(svc, "KiteTicker", _FakeTicker)
    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    await _seed_connected_account(db_session, connected=True, access_token="tok_1")
    inst = Instrument(
        exchange="NFO", symbol="NIFTY26SEP23000CE", name="NIFTY26SEP23000CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY26SEP23000CE",
    )
    db_session.add(inst)
    await db_session.commit()

    async def fake_get_instruments(self, segment="NSE"):
        return [{"tradingsymbol": "NIFTY26SEP23000CE", "instrument_token": "555"}]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_instruments", fake_get_instruments)

    engine = TickEngine()
    service = svc.KiteTickerService(engine)
    await service._refresh()
    first_ticker = _FakeTicker.instances[0]

    # Simulate the next day's fresh "Login with Zerodha" -- a new access_token.
    from sqlalchemy import select
    result = await db_session.execute(select(BrokerCredential))
    credential = result.scalar_one()
    from app.core.encryption import decrypt_payload
    creds = json.loads(decrypt_payload(credential.encrypted_payload))
    creds["access_token"] = "tok_2"
    credential.encrypted_payload = encrypt_payload(json.dumps(creds))
    await db_session.commit()

    await service._refresh()

    assert len(_FakeTicker.instances) == 2
    assert first_ticker.closed is True
    assert _FakeTicker.instances[1].access_token == "tok_2"


async def test_refresh_subscribes_nfo_and_nse_in_different_modes(db_session: AsyncSession, monkeypatch):
    _FakeTicker.instances.clear()
    monkeypatch.setattr(svc, "KiteTicker", _FakeTicker)
    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    await _seed_connected_account(db_session, connected=True, access_token="tok_a")
    option = Instrument(
        exchange="NFO", symbol="NIFTY26SEP23000CE", name="NIFTY26SEP23000CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY26SEP23000CE",
    )
    equity = Instrument(exchange="NSE", symbol="INFY", name="Infosys", instrument_type="equity", data_source="zerodha_kite", external_ref="INFY")
    db_session.add_all([option, equity])
    await db_session.commit()

    async def fake_get_instruments(self, segment="NSE"):
        if segment == "NFO":
            return [{"tradingsymbol": "NIFTY26SEP23000CE", "instrument_token": "555"}]
        return [{"tradingsymbol": "INFY", "instrument_token": "777"}]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_instruments", fake_get_instruments)

    engine = TickEngine()
    service = svc.KiteTickerService(engine)
    await service._refresh()

    ticker = _FakeTicker.instances[0]
    assert sorted(ticker.subscribed) == [555, 777]
    assert ("full", [555]) in ticker.mode_calls
    assert ("ltp", [777]) in ticker.mode_calls

    # A live NSE tick (no "oi" field at all -- LTP mode) updates price only.
    ticker.on_ticks(ticker, [{"instrument_token": 777, "last_price": 1500.0}])
    assert engine.get_current_price(equity.id) == 1500.0
    assert engine.get_current_oi(equity.id) is None


async def test_refresh_trims_nse_tokens_to_fit_subscription_cap_after_nfo(db_session: AsyncSession, monkeypatch):
    """NFO always gets priority within Kite's per-connection subscription
    cap -- NSE equities fill whatever budget remains instead of the whole
    connection failing outright once the combined catalog grows past it."""
    _FakeTicker.instances.clear()
    monkeypatch.setattr(svc, "KiteTicker", _FakeTicker)
    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    monkeypatch.setattr(svc, "MAX_SUBSCRIBE_TOKENS", 2)
    await _seed_connected_account(db_session, connected=True, access_token="tok_a")
    option = Instrument(
        exchange="NFO", symbol="NIFTY26SEP23000CE", name="NIFTY26SEP23000CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY26SEP23000CE",
    )
    equity_a = Instrument(exchange="NSE", symbol="INFY", name="Infosys", instrument_type="equity", data_source="zerodha_kite", external_ref="INFY")
    equity_b = Instrument(exchange="NSE", symbol="TCS", name="TCS", instrument_type="equity", data_source="zerodha_kite", external_ref="TCS")
    db_session.add_all([option, equity_a, equity_b])
    await db_session.commit()

    async def fake_get_instruments(self, segment="NSE"):
        if segment == "NFO":
            return [{"tradingsymbol": "NIFTY26SEP23000CE", "instrument_token": "555"}]
        return [
            {"tradingsymbol": "INFY", "instrument_token": "777"},
            {"tradingsymbol": "TCS", "instrument_token": "888"},
        ]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_instruments", fake_get_instruments)

    engine = TickEngine()
    service = svc.KiteTickerService(engine)
    await service._refresh()

    ticker = _FakeTicker.instances[0]
    # Budget is MAX_SUBSCRIBE_TOKENS(2) - len(nfo)(1) = 1 NSE slot only.
    assert len(ticker.subscribed) == 2
    assert 555 in ticker.subscribed
    nse_subscribed = [t for t in ticker.subscribed if t != 555]
    assert len(nse_subscribed) == 1


async def test_refresh_no_error_when_nothing_connected(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(svc, "KiteTicker", _FakeTicker)
    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    engine = TickEngine()
    service = svc.KiteTickerService(engine)
    await service._refresh()  # must not raise
    assert service.last_error == "No connected Zerodha account"


def test_stop_closes_ticker_but_never_calls_stop():
    """Confirms the module never calls KiteTicker.stop() (which would
    kill the shared Twisted reactor for the whole process) -- only
    .close(), per the module's own documented constraint."""
    engine = TickEngine()
    service = svc.KiteTickerService(engine)
    fake = _FakeTicker("k", "t")
    service._ticker = fake
    assert not hasattr(_FakeTicker, "stop")  # the fake doesn't even define one -- would AttributeError if called
    service.stop()
    assert fake.closed is True
