"""Idempotent seed data: RBAC roles, the initial administrator account, and
the broker catalog (PRD sections 5, 7, 48). Safe to run multiple times.

Usage: python -m app.seed
"""

import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.broker import Broker
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

        await db.commit()
        print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
