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
instruments) is filled in order of what's read live (see _select_tokens):
anything a strategy or an open page is reading, the indices, index
options of the nearest expiries and index futures, NSE stocks, stock
futures. Stock options aren't streamed unless something reads them -- the
F&O scan takes their open interest from Kite quotes and its own store --
since tens of thousands of them used to fill the cap by row order, leaving
no room for NIFTY options or any NSE stock. Something that starts reading
an instrument during the day is added to the open connection within a
minute (_add_in_use).

`KiteTicker` runs on Twisted's reactor, a process-wide singleton that can
only ever be started once per process: `connect(threaded=True)` starts it
in a daemon thread the first time; every subsequent `KiteTicker` instance
in this process just adds another connection to that SAME already-running
reactor (calling `.stop()` -- which stops the reactor for good -- would
break every future ticker in this process, so this module only ever calls
`.close()`, never `.stop()`). Once the reactor is running, a connection is
opened and closed from the reactor's own thread (_in_reactor), never from
FastAPI's: Twisted isn't thread-safe, and a connect() made from here sat
unattended in the idle reactor after the 15:30 close -- on 8 Oct the
morning reconnect never connected and nothing streamed all day (each
morning before had followed a deploy, i.e. the process's first connect).

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
from app.services.broker.zerodha_broker import IST, KiteAPIError, ZerodhaKiteBroker, resolve_tradingsymbol_with_be_fallback
from app.services.market_data.hours import nse_market_open
from app.services.market_data.tick_engine import TickEngine, tick_engine

logger = logging.getLogger(__name__)

# How often to check for a fresher access_token (the day's "Login with
# Zerodha"), a dead connection, and instruments something started reading
# that aren't streamed yet -- not how often ticks arrive, that's push-driven
# by Kite itself and can be many times a second.
REFRESH_INTERVAL_SECONDS = 60
# At most this often, an instrument being read that isn't in the resolved
# catalog yet (newly added) triggers re-reading the catalog.
RESOLVE_AGAIN_SECONDS = 300

# The segments this service subscribes to in one WS connection.
KITE_SUBSCRIBED_EXCHANGES = ("NFO", "NSE")

# Kite's documented per-WebSocket-connection subscription ceiling, filled
# in _select_tokens' order.
MAX_SUBSCRIBE_TOKENS = 3000

# Index underlyings (Kite's NFO "name"): their options of the nearest
# INDEX_EXPIRIES_LIVE expiries and their futures are streamed ahead of NSE
# stocks -- PCR, the NIFTY strategies and the Options page read them.
INDEX_NAMES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
INDEX_EXPIRIES_LIVE = 4


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
        segment_map: dict[int, uuid.UUID] = {}
        for instrument_id, external_ref in segment_rows:
            # Shared with zerodha_broker.get_historical_data's identical
            # -BE fallback -- a surveillance-moved stock (e.g. HEG, HFCL)
            # would otherwise silently lose live ticks here even though
            # its historical candles resolve fine.
            token = resolve_tradingsymbol_with_be_fallback(token_by_symbol, external_ref)
            if token is not None:
                segment_map[token] = instrument_id
        token_maps[segment] = segment_map
    return token_maps


async def _kite_rows_by_token(api_key: str) -> dict[int, dict]:
    """Kite's instrument-dump row for every token, both segments -- its
    "name" (an F&O contract's underlying), "instrument_type" (CE/PE/FUT/EQ),
    "expiry" and "segment" ("INDICES" for an index) decide the streaming
    order. The dumps are cached (zerodha_broker.get_instruments)."""
    broker = ZerodhaKiteBroker()
    broker._api_key = api_key
    rows: dict[int, dict] = {}
    for segment in KITE_SUBSCRIBED_EXCHANGES:
        try:
            dump = await broker.get_instruments(segment)
        except KiteAPIError:
            continue
        for row in dump:
            if row.get("instrument_token"):
                rows[int(row["instrument_token"])] = row
    return rows


def _select_tokens(
    token_maps: dict[str, dict[int, uuid.UUID]], subscriber_counts: dict[uuid.UUID, int], rows: dict[int, dict], today: str,
) -> dict[int, str]:
    """The tokens to stream, token -> segment, at most MAX_SUBSCRIBE_TOKENS,
    in this order:
      0. anything a strategy or an open page is reading right now
         (TickEngine's subscriber counts -- kite_rest_price_feed.py keys its
         polling off the same signal);
      1. NSE indices;
      2. index options of each index's nearest INDEX_EXPIRIES_LIVE expiries,
         and index futures;
      3. NSE stocks;
      4. stock futures;
      5. the rest (farther index options, rows Kite's dump doesn't describe).
    Stock options are left out unless something reads them. Within a tier,
    the catalog's own order."""
    nearest: dict[str, list[str]] = {}
    for token in token_maps.get("NFO", {}):
        row = rows.get(token) or {}
        if row.get("name") in INDEX_NAMES and row.get("instrument_type") in ("CE", "PE") and (row.get("expiry") or "") >= today:
            nearest.setdefault(row["name"], []).append(row["expiry"])
    nearest = {name: sorted(set(expiries))[:INDEX_EXPIRIES_LIVE] for name, expiries in nearest.items()}

    def tier(token: int, segment: str, instrument_id: uuid.UUID) -> int | None:
        if subscriber_counts.get(instrument_id, 0) > 0:
            return 0
        row = rows.get(token)
        if row is None:
            return 5
        if segment == "NSE":
            return 1 if row.get("segment") == "INDICES" else 3
        kind, name = row.get("instrument_type"), row.get("name")
        if name in INDEX_NAMES:
            if kind == "FUT" or (kind in ("CE", "PE") and row.get("expiry") in nearest.get(name, ())):
                return 2
            return 5
        if kind == "FUT":
            return 4
        if kind in ("CE", "PE"):
            return None  # a stock option nothing reads
        return 5

    ranked = []
    for segment in KITE_SUBSCRIBED_EXCHANGES:
        for token, instrument_id in token_maps.get(segment, {}).items():
            rank = tier(token, segment, instrument_id)
            if rank is not None:
                ranked.append((rank, len(ranked), token, segment))
    ranked.sort()
    if len(ranked) > MAX_SUBSCRIBE_TOKENS:
        logger.warning(
            "%d instruments to stream, Kite allows %d per connection -- the %d last in streaming order are left out",
            len(ranked), MAX_SUBSCRIBE_TOKENS, len(ranked) - MAX_SUBSCRIBE_TOKENS,
        )
    return {token: segment for _, _, token, segment in ranked[:MAX_SUBSCRIBE_TOKENS]}


def _in_reactor(fn) -> None:
    """KiteTicker's socket belongs to Twisted's reactor thread; a call into
    it from the asyncio loop is handed over rather than made here (which
    also wakes the reactor if it's idle). Before the reactor first runs --
    the process's first connect() starts it -- there's no thread to hand
    over to, and the call is made here."""
    from twisted.internet import reactor

    if reactor.running:
        reactor.callFromThread(fn)
    else:
        fn()


class KiteTickerService:
    def __init__(self, engine: TickEngine) -> None:
        self._engine = engine
        self._supervisor_task: asyncio.Task | None = None
        self._ticker: KiteTicker | None = None
        self._current_access_token: str | None = None
        self._token_map: dict[int, uuid.UUID] = {}  # every resolved token -> instrument
        self._candidates: dict[uuid.UUID, tuple[int, str]] = {}  # instrument -> (token, segment)
        self._subscribed: dict[int, str] = {}  # streamed token -> segment
        self._resolved_at: datetime | None = None
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
            _in_reactor(self._ticker.close)
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

    async def _refresh(self, now: datetime | None = None) -> None:
        if not nse_market_open(now or datetime.now(timezone.utc)):
            # Stay disconnected outside real trading hours -- Kite sends no
            # ticks while NSE is shut anyway, but leaving the connection up
            # meant TickEngine's set_real_price/set_real_oi kept whatever
            # they last held (no expiry on either, see tick_engine.py) and
            # every reader kept treating it as live. That's exactly how a
            # native strategy once opened and flat-closed a spread against
            # Friday's frozen price on a Saturday with nothing here to say
            # otherwise. Closing here means the next _refresh() cycle once
            # the market reopens reconnects cleanly, same as a cold start.
            if self._ticker is not None:
                _in_reactor(self._ticker.close)
                self._ticker = None
                self._current_access_token = None
            self.last_error = None
            return

        async with AsyncSessionLocal() as db:
            creds = await find_connected_zerodha_credentials(db)
            if creds is None:
                self.last_error = "No connected Zerodha account"
                return
            access_token = creds["access_token"]
            # Same-token dedup alone left a WebSocket that died mid-day
            # (a transient disconnect, or the once-observed 403 loop) never
            # rebuilt for the rest of the day: Kite's own auto-reconnect
            # only retries with the SAME token (see module docstring), and
            # this cycle only ever rebuilt on a genuinely NEW token from a
            # fresh login -- so a dead-but-same-token ticker silently sat
            # there with is_connected() == False forever, freezing OI at
            # whatever the last real tick was, hours before an expired
            # token would ever have been the actual explanation. Checking
            # is_connected() here makes every cycle self-healing
            # regardless of why the WS died, not just recoverable via a
            # fresh "Login with Zerodha".
            if access_token == self._current_access_token and self._ticker is not None and self._ticker.is_connected():
                # Already streaming with the current token: just add what
                # started being read since.
                await self._add_in_use(db, creds["api_key"])
                return
            token_maps = await _resolve_kite_token_map(db, creds["api_key"])
            rows = await _kite_rows_by_token(creds["api_key"])

        nfo_map = token_maps.get("NFO", {})
        nse_map = token_maps.get("NSE", {})
        if not nfo_map and not nse_map:
            self.last_error = "No NFO/NSE instruments to subscribe to (backfill one first)"
            return

        # Capped to Kite's per-connection limit: an oversized subscribe gets
        # the connection killed ("Message too big"), and every reconnect
        # re-requests the same list, so it never recovers on its own.
        today = (now or datetime.now(timezone.utc)).astimezone(IST).date().isoformat()
        selected = _select_tokens(token_maps, self._engine._subscriber_counts, rows, today)
        self._remember(token_maps)
        self._subscribed = selected

        old_ticker = self._ticker
        new_ticker = KiteTicker(creds["api_key"], access_token)
        new_ticker.on_ticks = self._on_ticks
        new_ticker.on_connect = self._on_connect
        new_ticker.on_close = self._on_close
        new_ticker.on_error = self._on_error
        _in_reactor(lambda: new_ticker.connect(threaded=True))

        self._ticker = new_ticker
        self._current_access_token = access_token
        if old_ticker is not None:
            _in_reactor(old_ticker.close)

    def _remember(self, token_maps: dict[str, dict[int, uuid.UUID]]) -> None:
        self._token_map = {token: iid for segment in token_maps.values() for token, iid in segment.items()}
        self._candidates = {iid: (token, seg) for seg, segment in token_maps.items() for token, iid in segment.items()}
        self._resolved_at = datetime.now(timezone.utc)

    @staticmethod
    def _stream(ws, tokens: dict[int, str]) -> None:
        nfo = [t for t, seg in tokens.items() if seg == "NFO"]
        nse = [t for t, seg in tokens.items() if seg == "NSE"]
        if nfo or nse:
            ws.subscribe(nfo + nse)
        if nfo:
            ws.set_mode(ws.MODE_FULL, nfo)  # Full mode is what carries OI for F&O
        if nse:
            ws.set_mode(ws.MODE_LTP, nse)  # Equities: last price only, no OI to carry

    def _on_connect(self, ws, response) -> None:
        # Everything streamed so far, additions included -- so a reconnect
        # keeps them.
        self._stream(ws, dict(self._subscribed))
        self.last_connected_at = datetime.now(timezone.utc)
        self.last_error = None

    async def _add_in_use(self, db, api_key: str) -> None:
        """Adds instruments a strategy or page started reading since the
        connection opened (a new option leg, a chart) to the open connection,
        while there's room under the cap."""
        in_use = [iid for iid, count in list(self._engine._subscriber_counts.items()) if count > 0]
        unknown = [iid for iid in in_use if iid not in self._candidates]
        now = datetime.now(timezone.utc)
        if unknown and (self._resolved_at is None or (now - self._resolved_at).total_seconds() >= RESOLVE_AGAIN_SECONDS):
            self._remember(await _resolve_kite_token_map(db, api_key))  # a newly added instrument
        room = MAX_SUBSCRIBE_TOKENS - len(self._subscribed)
        add: dict[int, str] = {}
        for iid in in_use:
            token, segment = self._candidates.get(iid, (None, None))
            if token is not None and token not in self._subscribed and token not in add and len(add) < room:
                add[token] = segment
        if not add:
            return
        self._subscribed = {**self._subscribed, **add}
        ticker = self._ticker
        _in_reactor(lambda: self._stream(ticker, add))
        logger.info("Kite ticker: streaming %d more instrument(s) something started reading", len(add))

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
