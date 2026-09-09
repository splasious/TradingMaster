"""Live Kite WebSocket ticker for NFO price + open interest -- feeds
`TickEngine` with genuine streaming data instead of Zerodha's simulated
random-walk fallback (see tick_engine.py's own docstring). Mirrors
real_price_feed.py's role for Delta, but push-streaming instead of
REST-polled, since Kite -- unlike Delta in this codebase -- has a real
ticker (`kiteconnect.KiteTicker`, the official SDK's tested binary-
protocol client; hand-rolling that parser ourselves was considered and
rejected, see the design plan this was built from -- OI's byte offset is
exactly the kind of detail we'd rather not get subtly wrong).

`KiteTicker` runs on Twisted's reactor, a process-wide singleton that can
only ever be started once per process: `connect(threaded=True)` starts it
in a daemon thread the first time; every subsequent `KiteTicker` instance
in this process just adds another connection to that SAME already-running
reactor (calling `connect()` again is safe; calling `.stop()` -- which
stops the reactor for good -- would break every future ticker in this
process, so this module only ever calls `.close()`, never `.stop()`).

Its callbacks (`on_ticks`, `on_connect`, ...) all run on that reactor
thread, never on FastAPI's own asyncio loop -- but `TickEngine.set_real_price`/
`set_real_oi` are plain synchronous dict writes with no `await` anywhere
in them, so calling them directly from the reactor thread is safe
(CPython's GIL protects a single dict-item write) without needing an
asyncio.Queue bridge.

Kite's own auto-reconnect (built into KiteTicker) only ever retries with
the SAME access_token it was constructed with -- it cannot recover from
that token expiring (Kite's daily ~6am IST session expiry, see
zerodha_broker.py's module docstring, and the reconnect bug fixed
earlier in app/services/broker/zerodha_broker.py's authenticate()). The
periodic `_refresh` cycle here checks for a NEWER access_token in storage
(the result of the user's next "Login with Zerodha") and, when one
appears, builds a fresh `KiteTicker` and swaps it in -- closing the old
one, never touching the shared reactor.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from kiteconnect import KiteTicker
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.encryption import decrypt_payload
from app.db.session import AsyncSessionLocal
from app.models.broker import Broker, BrokerAccount, BrokerConnection, ConnectionStatus
from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker
from app.services.market_data.tick_engine import TickEngine, tick_engine

logger = logging.getLogger(__name__)

# How often to check for a fresher access_token (the day's "Login with
# Zerodha") and re-resolve the NFO instrument set (a newly-backfilled
# contract) -- not how often ticks arrive, that's push-driven by Kite
# itself and can be many times a second.
REFRESH_INTERVAL_SECONDS = 300


async def _find_connected_credentials(db) -> dict | None:
    """Decrypted credentials for the current CONNECTED zerodha_kite
    account, or None if none is connected -- the exact account-selection
    query KiteSessionMonitorScheduler already uses (kite_session_monitor.py)."""
    result = await db.execute(
        select(BrokerAccount)
        .join(Broker, Broker.id == BrokerAccount.broker_id)
        .join(BrokerConnection, BrokerConnection.broker_account_id == BrokerAccount.id)
        .options(selectinload(BrokerAccount.credential))
        .where(Broker.code == "zerodha_kite", BrokerConnection.status == ConnectionStatus.CONNECTED.value)
    )
    account = result.scalars().first()
    if account is None or account.credential is None:
        return None
    creds = json.loads(decrypt_payload(account.credential.encrypted_payload))
    if not creds.get("api_key") or not creds.get("access_token"):
        return None
    return creds


async def _resolve_nfo_token_map(db, api_key: str) -> dict[int, uuid.UUID]:
    """Kite numeric instrument_token -> this app's Instrument.id, for
    every currently-backfilled NFO contract. Re-resolved on every refresh
    cycle so a newly-backfilled contract is picked up automatically.

    Only needs api_key, not a full authenticated session: Kite's
    instrument-dump endpoint is public catalog data (confirmed live
    earlier this session, and in zerodha_broker.py's own module
    docstring) -- no /user/profile round-trip needed just to list
    tradingsymbol -> instrument_token."""
    result = await db.execute(select(Instrument.id, Instrument.external_ref).where(Instrument.exchange == "NFO"))
    rows = result.all()
    if not rows:
        return {}
    broker = ZerodhaKiteBroker()
    broker._api_key = api_key
    try:
        nfo_rows = await broker.get_instruments("NFO")
    except KiteAPIError:
        logger.exception("Could not fetch Kite's NFO instrument dump for token resolution")
        return {}
    token_by_symbol = {row["tradingsymbol"]: int(row["instrument_token"]) for row in nfo_rows if row.get("instrument_token")}
    return {
        token_by_symbol[external_ref]: instrument_id
        for instrument_id, external_ref in rows
        if external_ref in token_by_symbol
    }


class KiteTickerService:
    def __init__(self, engine: TickEngine) -> None:
        self._engine = engine
        self._supervisor_task: asyncio.Task | None = None
        self._ticker: KiteTicker | None = None
        self._current_access_token: str | None = None
        self._token_map: dict[int, uuid.UUID] = {}
        self.last_connected_at: datetime | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._supervisor_task is None:
            self._supervisor_task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            self._supervisor_task = None
        if self._ticker is not None:
            self._ticker.close()
            self._ticker = None

    @property
    def running(self) -> bool:
        return self._supervisor_task is not None

    async def _run(self) -> None:
        while True:
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Kite ticker refresh failed")
                self.last_error = "refresh failed, see logs"
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

    async def _refresh(self) -> None:
        async with AsyncSessionLocal() as db:
            creds = await _find_connected_credentials(db)
            if creds is None:
                self.last_error = "No connected Zerodha account"
                return
            access_token = creds["access_token"]
            if access_token == self._current_access_token and self._ticker is not None:
                return  # already streaming with the current token, nothing to do
            token_map = await _resolve_nfo_token_map(db, creds["api_key"])

        if not token_map:
            self.last_error = "No NFO instruments to subscribe to (backfill one first)"
            return

        old_ticker = self._ticker
        self._token_map = token_map
        new_ticker = KiteTicker(creds["api_key"], access_token)
        new_ticker.on_ticks = self._on_ticks
        new_ticker.on_connect = self._make_on_connect(list(token_map.keys()))
        new_ticker.on_close = self._on_close
        new_ticker.on_error = self._on_error
        new_ticker.connect(threaded=True)

        self._ticker = new_ticker
        self._current_access_token = access_token
        if old_ticker is not None:
            old_ticker.close()

    def _make_on_connect(self, tokens: list[int]):
        def _on_connect(ws, response) -> None:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)  # Full mode is what carries OI for F&O
            self.last_connected_at = datetime.now(timezone.utc)
            self.last_error = None

        return _on_connect

    def _on_ticks(self, ws, ticks: list[dict]) -> None:
        # Runs on KiteTicker's own (Twisted reactor) thread -- see module
        # docstring for why calling TickEngine's plain synchronous setters
        # from here is safe.
        for tick in ticks:
            instrument_id = self._token_map.get(tick.get("instrument_token"))
            if instrument_id is None:
                continue
            price = tick.get("last_price")
            if price:
                self._engine.set_real_price(instrument_id, price, source="kite")
            oi = tick.get("oi")
            if oi is not None:
                self._engine.set_real_oi(instrument_id, oi)

    def _on_close(self, ws, code, reason) -> None:
        logger.warning("Kite ticker connection closed: %s %s", code, reason)

    def _on_error(self, ws, code, reason) -> None:
        logger.error("Kite ticker connection error: %s %s", code, reason)
        self.last_error = f"{code}: {reason}"


kite_ticker_service = KiteTickerService(tick_engine)
