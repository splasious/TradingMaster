"""Delta Exchange is hidden from the site by default (services/visibility.py):
its data stays in the database, but no API lists, serves or accepts it."""

import importlib.util
import pathlib
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.backfill_platform import BfBackfillJob, BfSettings, BfSymbol, BfWatchlist, BfWatchlistItem
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential
from app.models.instrument import Instrument
from app.models.live_trading import LiveOrder
from app.models.user import User

MIGRATION = pathlib.Path(__file__).parent.parent / "alembic/versions/c2d3e4f5a6b7_delete_delta_broker_accounts.py"


async def _headers(client: AsyncClient, admin: dict) -> dict:
    resp = await client.post("/api/v1/auth/login", json={"email": admin["email"], "password": admin["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _admin_user(db: AsyncSession) -> User:
    return (await db.execute(select(User).where(User.email == "admin@tradingmaster.internal"))).scalar_one()


async def _delta_account(db: AsyncSession, user: User, label: str) -> BrokerAccount:
    broker = (await db.execute(select(Broker).where(Broker.code == "delta_exchange"))).scalar_one()
    account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label=label, environment="live")
    db.add(account)
    await db.flush()
    db.add_all([BrokerCredential(broker_account_id=account.id, encrypted_payload="x"), BrokerConnection(broker_account_id=account.id)])
    return account


async def test_instruments_leave_out_delta(client, seeded_admin, db_session):
    nse = Instrument(exchange="NSE", symbol="INFY", name="Infosys", instrument_type="equity", data_source="zerodha_kite", external_ref="INFY")
    delta = Instrument(exchange="DELTA", symbol="PAXGUSD", name="PAX Gold", instrument_type="perpetual_future", data_source="delta_exchange", external_ref="PAXGUSD")
    db_session.add_all([nse, delta])
    await db_session.commit()
    headers = await _headers(client, seeded_admin)

    listed = (await client.get("/api/v1/instruments", headers=headers)).json()
    assert [i["symbol"] for i in listed] == ["INFY"]
    assert (await client.get("/api/v1/instruments", params={"exchange": "DELTA"}, headers=headers)).json() == []
    assert (await client.get(f"/api/v1/instruments/{delta.id}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/instruments/{nse.id}", headers=headers)).status_code == 200
    assert (await client.post("/api/v1/instruments/sync/delta_exchange", headers=headers)).status_code == 400
    assert (await db_session.get(Instrument, delta.id)) is not None  # kept, only hidden


async def test_backfill_platform_refuses_and_leaves_out_delta(client, seeded_admin, db_session):
    headers = await _headers(client, seeded_admin)
    user = await _admin_user(db_session)
    gold = BfSymbol(source="delta", symbol="PAXGUSD", display_name="PAX Gold")
    infy = BfSymbol(source="zerodha", symbol="INFY", display_name="Infosys")
    db_session.add_all([gold, infy])
    await db_session.flush()
    delta_job = BfBackfillJob(symbol_id=gold.id, source="delta", timeframe="1d")
    nse_job = BfBackfillJob(symbol_id=infy.id, source="zerodha", timeframe="1d")
    delta_list = BfWatchlist(owner_id=user.id, name="Delta Metals (Gold/Silver Tokens)")
    mixed = BfWatchlist(owner_id=user.id, name="Mixed")
    db_session.add_all([delta_job, nse_job, delta_list, mixed])
    await db_session.flush()
    db_session.add_all([
        BfWatchlistItem(watchlist_id=delta_list.id, symbol_id=gold.id),
        BfWatchlistItem(watchlist_id=mixed.id, symbol_id=gold.id),
        BfWatchlistItem(watchlist_id=mixed.id, symbol_id=infy.id),
    ])
    await db_session.commit()

    for path in ("/sources/delta/status", "/sources/delta/timeframes", "/sources/delta/symbols?q=PAX"):
        assert (await client.get(f"/api/v1/backfill-platform{path}", headers=headers)).status_code == 400, path
    job = {"source": "delta", "symbol": "PAXGUSD", "display_name": "PAX Gold", "timeframe": "1d"}
    assert (await client.post("/api/v1/backfill-platform/jobs", json=job, headers=headers)).status_code == 400

    jobs = (await client.get("/api/v1/backfill-platform/jobs", headers=headers)).json()
    assert [j["source"] for j in jobs] == ["zerodha"]
    assert (await client.get(f"/api/v1/backfill-platform/jobs/{delta_job.id}", headers=headers)).status_code == 404

    lists = (await client.get("/api/v1/backfill-platform/watchlists", headers=headers)).json()
    assert [(w["name"], w["symbol_count"]) for w in lists] == [("Mixed", 1)]
    assert (await client.get(f"/api/v1/backfill-platform/watchlists/{delta_list.id}/items", headers=headers)).status_code == 404
    items = (await client.get(f"/api/v1/backfill-platform/watchlists/{mixed.id}/items", headers=headers)).json()
    assert [i["symbol"] for i in items] == ["INFY"]
    csv_out = (await client.get(f"/api/v1/backfill-platform/watchlists/{mixed.id}/export.csv", headers=headers)).text
    assert "PAXGUSD" not in csv_out and "INFY" in csv_out

    upload = "source,symbol,display_name\ndelta,XAUTUSD,Tether Gold\n"
    result = (await client.post(
        f"/api/v1/backfill-platform/watchlists/{mixed.id}/import", files={"file": ("w.csv", upload, "text/csv")}, headers=headers,
    )).json()
    assert result == {"added": 0, "skipped": 1}


async def test_schedule_save_leaves_the_hidden_delta_switch_alone(client, seeded_admin, db_session):
    db_session.add(BfSettings(id=1, delta_enabled=False))
    await db_session.commit()
    headers = await _headers(client, seeded_admin)
    schedule = (await client.get("/api/v1/backfill-platform/schedule", headers=headers)).json()
    body = {k: schedule[k] for k in ("auto_topup_zerodha", "auto_topup_zerodha_nfo", "topup_time", "live_start", "live_end", "topup_timeframes")}

    resp = await client.put("/api/v1/backfill-platform/schedule", json=body | {"delta_enabled": True}, headers=headers)
    assert resp.status_code == 200 and resp.json()["delta_enabled"] is False


async def test_brokers_leave_out_delta(client, seeded_admin, db_session):
    headers = await _headers(client, seeded_admin)
    await _delta_account(db_session, await _admin_user(db_session), "Primary")
    await db_session.commit()

    codes = [b["code"] for b in (await client.get("/api/v1/brokers", headers=headers)).json()]
    assert "delta_exchange" not in codes and "zerodha_kite" in codes
    assert (await client.get("/api/v1/brokers/accounts", headers=headers)).json() == []
    resp = await client.post(
        "/api/v1/brokers/accounts",
        json={"broker_code": "delta_exchange", "account_label": "X", "environment": "live", "credentials": {"api_key": "k", "api_secret": "s"}},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_brokers_show_delta_when_switched_back_on(client, seeded_admin, db_session, show_delta):
    headers = await _headers(client, seeded_admin)
    await _delta_account(db_session, await _admin_user(db_session), "Primary")
    await db_session.commit()

    assert "delta_exchange" in [b["code"] for b in (await client.get("/api/v1/brokers", headers=headers)).json()]
    assert len((await client.get("/api/v1/brokers/accounts", headers=headers)).json()) == 1


async def test_migration_deletes_unused_delta_accounts_only(seeded_admin, db_engine, db_session):
    user = await _admin_user(db_session)
    unused = await _delta_account(db_session, user, "Primary")
    traded = await _delta_account(db_session, user, "Has an order")
    db_session.add(LiveOrder(broker_account_id=traded.id, client_order_id=uuid.uuid4().hex, side="buy", quantity=1, status="filled"))
    zerodha = (await db_session.execute(select(Broker).where(Broker.code == "zerodha_kite"))).scalar_one()
    kite = BrokerAccount(user_id=user.id, broker_id=zerodha.id, account_label="Kite", environment="live")
    db_session.add(kite)
    await db_session.commit()
    unused_id, traded_id, kite_id = unused.id, traded.id, kite.id

    spec = importlib.util.spec_from_file_location("delete_delta_broker_accounts", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    async with db_engine.begin() as conn:
        await conn.run_sync(run)

    db_session.expire_all()
    remaining = set((await db_session.execute(select(BrokerAccount.id))).scalars().all())
    assert remaining == {traded_id, kite_id}
    creds = set((await db_session.execute(select(BrokerCredential.broker_account_id))).scalars().all())
    conns = set((await db_session.execute(select(BrokerConnection.broker_account_id))).scalars().all())
    assert unused_id not in creds and unused_id not in conns
    logs = (await db_session.execute(select(AuditLog).where(AuditLog.action == "BROKER_ACCOUNT_DELETED"))).scalars().all()
    assert [(log.object_id, log.previous_value) for log in logs] == [
        (str(unused_id), {"broker_code": "delta_exchange", "account_label": "Primary", "environment": "live"}),
    ]
