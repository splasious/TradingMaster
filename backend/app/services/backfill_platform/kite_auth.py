"""Shared helper: an authenticated ZerodhaKiteBroker for a given user's own
connected account, reused by both symbol search and backfill (Kite's
instrument list and historical candles both require auth, unlike Yahoo's
sidecar service or Delta's public API)."""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_payload
from app.models.broker import Broker, BrokerAccount, BrokerCredential
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker


async def get_authenticated_kite_broker(db: AsyncSession, user_id) -> ZerodhaKiteBroker:
    account = (
        await db.execute(
            select(BrokerAccount)
            .join(Broker, Broker.id == BrokerAccount.broker_id)
            .where(BrokerAccount.user_id == user_id, Broker.code == "zerodha_kite")
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
