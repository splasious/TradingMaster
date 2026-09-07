"""Shared helper: an authenticated ZerodhaKiteBroker for a given user's own
connected account, reused by both symbol search and backfill (Kite's
instrument list and historical candles both require auth, unlike Delta's
public API)."""

import json

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.encryption import decrypt_payload
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker


async def get_authenticated_kite_broker(db: AsyncSession, user_id) -> ZerodhaKiteBroker:
    # A user can end up with more than one zerodha_kite BrokerAccount row
    # (e.g. reconnecting via "Connect Broker" instead of "Login with
    # Zerodha" on the existing one, after the old session expired) -- with
    # no ordering, plain .first() returned whichever row Postgres happened
    # to scan first, which in practice was the stale/broken one, not the
    # freshly reconnected account. Prefer a CONNECTED account, and among
    # those (or if none are connected) the most recently created.
    account = (
        await db.execute(
            select(BrokerAccount)
            .join(Broker, Broker.id == BrokerAccount.broker_id)
            .outerjoin(BrokerConnection, BrokerConnection.broker_account_id == BrokerAccount.id)
            .options(selectinload(BrokerAccount.connection))
            .where(BrokerAccount.user_id == user_id, Broker.code == "zerodha_kite")
            .order_by(
                case((BrokerConnection.status == ConnectionStatus.CONNECTED.value, 0), else_=1),
                BrokerAccount.created_at.desc(),
            )
        )
    ).scalars().first()
    if account is None:
        raise KiteAPIError("No Zerodha Kite account connected. Connect one in Settings > Brokers first.")

    credential = (
        await db.execute(select(BrokerCredential).where(BrokerCredential.broker_account_id == account.id))
    ).scalar_one_or_none()
    if credential is None:
        raise KiteAPIError("No credentials stored for the connected Zerodha Kite account.")

    creds = json.loads(decrypt_payload(credential.encrypted_payload))
    broker = ZerodhaKiteBroker()
    await broker.authenticate(creds)  # KiteLoginRequired/KiteAPIError propagate as-is -- caller decides how to show it
    return broker
