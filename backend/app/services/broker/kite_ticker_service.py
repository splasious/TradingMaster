"""Live Kite WebSocket ticker for NFO price + open interest, and NSE equity/
index prices -- feeds `TickEngine` with genuine streaming data instead of
Zerodha's simulated random-walk fallback (see tick_engine.py's own
docstring). Mirrors real_price_feed.py's role for Delta, but push-streaming
instead of REST-polled, since Kite -- unlike Delta in this codebase -- has
a real ticker (`kiteconnect.KiteTicker`, the official SDK's tested binary-
protocol client; hand-rolling that parser ourselves was considered and
rejected, see the design plan this was built from -- OI's byte offset is
exactly the kind of detail we'd rather not get subtly wrong).

Subscribes two segments in the same connection: NFO (options/futures,
MODE_FULL -- the only mode that carries open interest) and NSE (equities/
indices, MODE_LTP -- cheaper, and equities have no OI to carry here
anyway). Kite's own per-connection subscription cap (documented at 3000
instruments) is enforced with NFO given priority, since OI/F&O is this
service's original purpose -- NSE equities fill whatever budget remains
rather than failing the whole connection outright (see MAX_SUBSCRIBE_TOKENS).

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
# Zerodha") and re-resolve the NFO/NSE instrument set (a newly-backfilled
# contract or equity) -- not how often ticks arrive, that's push-driven by
# Kite itself and can be many times a second.
REFRESH_INTERVAL_SECONDS = 300

# The segments this service subscribes to in one WS connection, and the
# order in which they're prioritized when trimming to fit Kite's
# per-connection subscription cap (NFO first -- see MAX_SUBSCRIBE_TOKENS).
KITE_SUBSCRIBED_EXCHANGES = ("NFO", "NSE")

# Kite's documented per-WebSocket-connection subscription ceiling. NFO
# (OI, this service's original purpose) always gets priority within this
# budget; NSE equities fill whatever's left rather than the whole
# connection failing outright once the combined catalog grows past it.
MAX_SUBSCRIBE_TOKENS = 3000


async def find_connected_zerodha_account(db) -> BrokerAccount | None:
    """The current CONNECTED zerodha_kite BrokerAccount (with its
    credential relationship loaded), or None if none is connected -- the
    exact account-selection query KiteSessionMonitorScheduler already uses
    (kite_session_monitor.py). Shared (not module-private) since both
    app/services/options/history_depth.py and
    app/services/backfill_platform/nfo_expiry_rotation.py also need it --
    the latter for `.user_id` too (BfBackfillJob.requested_by), not just
    credentials, since it queues real backfill jobs rather than just
    reading candles directly."""
    result = await db.execute(
        select(BrokerAccount)
        .join(Broker, Broker.id == BrokerAccount.broker_id)
        .join(BrokerConnection, BrokerConnection.broker_account_id == BrokerAccount.id)
        .options(selectinload(BrokerAccount.credential))
        .where(Broker.code == "zerodha_kite", BrokerConnection.status == ConnectionStatus.CONNECTED.value)
    )
    return result.scalars().first()


async def find_connected_zerodha_credentials(db) -> dict | None:
    """Decrypted credentials for the current CONNECTED zerodha_kite
    account, or None if none is connected/has no usable credential yet."""
    account = await find_connected_zerodha_account(db)
    if account is None or account.credential is None:
        return None
    creds = json.loads(decrypt_payload(account.credential.encrypted_payload))
    if not creds.get("api_key") or not creds.get("access_token"):
        return None
    return creds


async def diagnose_zerodha_connection(db) -> dict:
    """Safe, secret-free breakdown of exactly which step of
    find_connected_zerodha_credentials is failing -- counts and booleans
    only, never a credential value, so this is safe to expose on the
    public /system/health endpoint. Exists because "No connected Zerodha
    account" is set from a single branch covering three different real
    causes (no CONNECTED BrokerConnection row at all, a connected account
    with no credential row, or a credential whose payload is missing
    api_key/access_token) -- indistinguishable from the outside without
    this, which made a real "shows Connected in Settings but the ticker
    still says disconnected" report impossible to root-cause without
    direct DB access."""
    result = await db.execute(
        select(BrokerAccount)
        .join(Broker, Broker.id == BrokerAccount.broker_id)
        .join(BrokerConnection, BrokerConnection.broker_account_id == BrokerAccount.id)
        .options(selectinload(BrokerAccount.credential))
        .where(Broker.code == "zerodha_kite", BrokerConnection.status == ConnectionStatus.CONNECTED.value)
    )
    accounts = result.scalars().all()
    if not accounts:
        return {"connected_accounts": 0}

    account = accounts[0]
    diagnosis: dict = {"connected_accounts": len(accounts), "has_credential": account.credential is not None}
    if account.credential is not None:
        try:
            creds = json.loads(decrypt_payload(account.credential.encrypted_payload))
            diagnosis["has_api_key"] = bool(creds.get("api_key"))
            diagnosis["has_access_token"] = bool(creds.get("access_token"))
        except Exception as exc:
            diagnosis["decrypt_error"] = type(exc).__name__
    return diagnosis


async def _resolve_kite_token_map(db, api_key: str) -> dict[str, dict[int, uuid.UUID]]:
    """Kite numeric instrument_token -> this app's Instrument.id, grouped by
    segment ("NFO", "NSE") since each is subscribed in a different WS mode
    (NFO -> MODE_FULL, the only mode carrying OI; NSE equities -> MODE_LTP,
    cheaper and sufficient since equities carry no OI here). Re-resolved on
    every refresh cycle so a newly-backfilled contract or equity is picked
    up automatically.

    Only needs api_key, not a full authenticated session: Kite's
    instrument-dump endpoint is public catalog data (confirmed live
    earlier this session, and in zerodha_broker.py's own module
    docstring) -- no /user/profile round-trip needed just to list
    tradingsymbol -> instrument_token. Scoped to data_source ==
    "zerodha_kite" -- an NSE row synced from a retired source (e.g. the
    old "yahoo_nse" adapter) has a tradingsymbol format Kite's own dump
    would never match anyway."""
    result = await db.execute(
        select(Instrument.id, Instrument.external_ref, Instrument.exchange).where(
            Instrument.exchange.in_(KITE_SUBSCRIBED_EXCHANGES), Instrument.data_source == "zerodha_kite"
        )
    )
    rows = result.all()
    if not rows:
        return {}

    broker = ZerodhaKiteBroker()
    broker._api_key = api_key
    token_maps: dict[str, dict[int, uuid.UUID]] = {}
    for segment in KITE_SUBSCRIBED_EXCHANGES:
        segment_rows = [(instrument_id, external_ref) for instrument_id, external_ref, exchange in rows if exchange == segment]
        if not segment_rows:
            continue
        try:
            dump = await broker.get_instruments(segment)
        except KiteAPIError:
            logger.exception("Could not fetch Kite's %s instrument dump for token resolution", segment)
            continue
        token_by_symbol = {row["tradingsymbol"]: int(row["instrument_token"]) for row in dump if row.get("instrument_token")}
        token_maps[segment] = {
            token_by_symbol[external_ref]: instrument_id
            for instrument_id, external_ref in segment_rows
            if external_ref in token_by_symbol
        }
    return token_maps


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
            creds = await find_connected_zerodha_credentials(db)
            if creds is None:
                self.last_error = "No connected Zerodha account"
                return
            access_token = creds["access_token"]
            if access_token == self._current_access_token and self._ticker is not None:
                return  # already streaming with the current token, nothing to do
            token_maps = await _resolve_kite_token_map(db, creds["api_key"])

        nfo_map = token_maps.get("NFO", {})
        nse_map = token_maps.get("NSE", {})
        if not nfo_map and not nse_map:
            self.last_error = "No NFO/NSE instruments to subscribe to (backfill one first)"
            return

        nse_items = list(nse_map.items())
        budget = max(0, MAX_SUBSCRIBE_TOKENS - len(nfo_map))
        if len(nse_items) > budget:
            logger.warning(
                "NSE token count (%d) exceeds the remaining subscription budget (%d of Kite's %d-token cap, "
                "after %d NFO) -- subscribing to the first %d only",
                len(nse_items), budget, MAX_SUBSCRIBE_TOKENS, len(nfo_map), budget,
            )
            nse_items = nse_items[:budget]
        nse_map = dict(nse_items)

        old_ticker = self._ticker
        self._token_map = {**nfo_map, **nse_map}
        new_ticker = KiteTicker(creds["api_key"], access_token)
        new_ticker.on_ticks = self._on_ticks
        new_ticker.on_connect = self._make_on_connect(list(nfo_map.keys()), list(nse_map.keys()))
        new_ticker.on_close = self._on_close
        new_ticker.on_error = self._on_error
        new_ticker.connect(threaded=True)

        self._ticker = new_ticker
        self._current_access_token = access_token
        if old_ticker is not None:
            old_ticker.close()

    def _make_on_connect(self, nfo_tokens: list[int], nse_tokens: list[int]):
        def _on_connect(ws, response) -> None:
            all_tokens = nfo_tokens + nse_tokens
            if all_tokens:
                ws.subscribe(all_tokens)
            if nfo_tokens:
                ws.set_mode(ws.MODE_FULL, nfo_tokens)  # Full mode is what carries OI for F&O
            if nse_tokens:
                ws.set_mode(ws.MODE_LTP, nse_tokens)  # Equities: last price only, no OI to carry
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
