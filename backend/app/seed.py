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
from app.models.user import Role, User, UserRole

ROLES = ["administrator", "trader", "analyst", "viewer"]
BROKERS = [
    ("zerodha_kite", "Zerodha Kite"),
    ("delta_exchange", "Delta Exchange"),
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
