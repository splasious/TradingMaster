"""Idempotent seed data: RBAC roles, the initial administrator account, and
the broker catalog (PRD sections 5, 7, 48). Safe to run multiple times.

Usage: python -m app.seed
"""

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.core.encryption import encrypt_payload
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.models.instrument import Instrument
from app.models.user import Role, User, UserRole

ROLES = ["administrator", "trader", "analyst", "viewer"]
BROKERS = [
    ("zerodha_kite", "Zerodha Kite"),
    ("delta_exchange", "Delta Exchange"),
]

# A curated starter catalog so the app is useful without first running
# "Sync from source" (which pulls hundreds of instruments). exchange+symbol
# is unique -- Sync backfills the rest without duplicating these.
NSE_INSTRUMENTS = [
    ("NIFTY 50", "Nifty 50 Index", "index"),
    ("NIFTY BANK", "Nifty Bank Index", "index"),
    ("RELIANCE", "Reliance Industries Ltd", "equity"),
    ("TCS", "Tata Consultancy Services Ltd", "equity"),
    ("HDFCBANK", "HDFC Bank Ltd", "equity"),
    ("INFY", "Infosys Ltd", "equity"),
    ("ICICIBANK", "ICICI Bank Ltd", "equity"),
    ("HINDUNILVR", "Hindustan Unilever Ltd", "equity"),
    ("ITC", "ITC Ltd", "equity"),
    ("SBIN", "State Bank of India", "equity"),
    ("BHARTIARTL", "Bharti Airtel Ltd", "equity"),
    ("KOTAKBANK", "Kotak Mahindra Bank Ltd", "equity"),
    ("LT", "Larsen & Toubro Ltd", "equity"),
    ("AXISBANK", "Axis Bank Ltd", "equity"),
    ("BAJFINANCE", "Bajaj Finance Ltd", "equity"),
    ("MARUTI", "Maruti Suzuki India Ltd", "equity"),
    ("ASIANPAINT", "Asian Paints Ltd", "equity"),
    ("HCLTECH", "HCL Technologies Ltd", "equity"),
    ("SUNPHARMA", "Sun Pharmaceutical Industries Ltd", "equity"),
    ("TITAN", "Titan Company Ltd", "equity"),
]

DELTA_INSTRUMENTS = [
    ("BTCUSD", "Bitcoin Perpetual"),
    ("ETHUSD", "Ethereum Perpetual"),
    ("SOLUSD", "Solana Perpetual"),
]


async def seed() -> None:
    settings = get_settings()

    async with AsyncSessionLocal() as db:
        role_by_name: dict[str, Role] = {}
        for name in ROLES:
            existing = (await db.execute(select(Role).where(Role.name == name))).scalar_one_or_none()
            if existing is None:
                existing = Role(name=name, description=f"{name.capitalize()} role")
                db.add(existing)
                await db.flush()
            role_by_name[name] = existing

        for code, name in BROKERS:
            existing = (await db.execute(select(Broker).where(Broker.code == code))).scalar_one_or_none()
            if existing is None:
                db.add(Broker(code=code, name=name, is_enabled=True))

        existing_instruments = {
            (row[0], row[1]) for row in (await db.execute(select(Instrument.exchange, Instrument.symbol))).all()
        }
        for symbol, name, itype in NSE_INSTRUMENTS:
            if ("NSE", symbol) in existing_instruments:
                continue
            db.add(
                Instrument(
                    exchange="NSE", symbol=symbol, name=name, instrument_type=itype,
                    data_source="yahoo_nse", external_ref=symbol,
                )
            )
        for symbol, name in DELTA_INSTRUMENTS:
            if ("DELTA", symbol) in existing_instruments:
                continue
            db.add(
                Instrument(
                    exchange="DELTA", symbol=symbol, name=name, instrument_type="perpetual_future",
                    data_source="delta_exchange", external_ref=symbol,
                )
            )

        admin = (
            await db.execute(select(User).where(User.email == settings.seed_admin_email))
        ).scalar_one_or_none()
        if admin is None:
            admin = User(
                email=settings.seed_admin_email,
                hashed_password=hash_password(settings.seed_admin_password),
                full_name="System Administrator",
            )
            admin.user_roles = [UserRole(role=role_by_name["administrator"])]
            db.add(admin)
            print(f"Created admin user: {settings.seed_admin_email}")
        else:
            print(f"Admin user already exists: {settings.seed_admin_email}")

        await db.flush()

        if settings.delta_api_key and settings.delta_api_secret:
            await _provision_delta_account(db, admin)

        await db.commit()
        print("Seed complete.")


async def _provision_delta_account(db, admin: User) -> None:
    """If DELTA_API_KEY/DELTA_API_SECRET are set in .env, store them
    encrypted via the same broker_credentials mechanism the Settings >
    Brokers UI uses (never as plaintext in the database) and attempt one
    real authenticated call to set an honest initial connection status."""
    from app.services.broker.registry import get_broker_adapter

    settings = get_settings()
    delta = (await db.execute(select(Broker).where(Broker.code == "delta_exchange"))).scalar_one_or_none()
    if delta is None:
        return

    existing = (
        await db.execute(
            select(BrokerAccount).where(BrokerAccount.user_id == admin.id, BrokerAccount.broker_id == delta.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        print("Delta Exchange broker account already provisioned, skipping.")
        return

    credentials = {"api_key": settings.delta_api_key, "api_secret": settings.delta_api_secret}
    account = BrokerAccount(user_id=admin.id, broker_id=delta.id, account_label="Primary", environment="live")
    db.add(account)
    await db.flush()
    db.add(BrokerCredential(broker_account_id=account.id, encrypted_payload=encrypt_payload(json.dumps(credentials))))

    connection = BrokerConnection(broker_account_id=account.id, status=ConnectionStatus.CONNECTING.value)
    db.add(connection)
    await db.flush()

    try:
        adapter = get_broker_adapter("delta_exchange")
        if await adapter.authenticate(credentials):
            await adapter.connect()
            connection.status = ConnectionStatus.CONNECTED.value
            connection.last_heartbeat_at = datetime.now(timezone.utc)
            print("Delta Exchange broker account provisioned and connected.")
        else:
            connection.status = ConnectionStatus.ERROR.value
            connection.last_error = "Authentication rejected by broker"
            print("Delta Exchange broker account provisioned, but authentication was rejected.")
    except Exception as exc:
        connection.status = ConnectionStatus.ERROR.value
        connection.last_error = str(exc)
        print(f"Delta Exchange broker account provisioned, but connection failed: {exc}")


if __name__ == "__main__":
    asyncio.run(seed())
