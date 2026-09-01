import asyncio
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import decode_token
from app.db.session import AsyncSessionLocal
from app.services.market_data.real_price_feed import real_price_feed
from app.services.market_data.seed_price import get_seed_price
from app.services.market_data.tick_engine import tick_engine

router = APIRouter()


async def _seed_price(instrument_id: uuid.UUID) -> float:
    async with AsyncSessionLocal() as db:
        return await get_seed_price(db, instrument_id)


@router.websocket("/market-data")
async def market_data_ws(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    payload = decode_token(token) if token else None
    if payload is None or payload.get("type") != "access":
        await websocket.close(code=4401)
        return

    await websocket.accept()
    queue = tick_engine.register()
    subscribed: set[uuid.UUID] = set()

    await websocket.send_json({"type": "connection_status", "status": "connected"})

    async def receiver() -> None:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")
            ids = {uuid.UUID(i) for i in message.get("instrument_ids", [])}
            if msg_type == "subscribe":
                for instrument_id in ids - subscribed:
                    tick_engine.subscribe(instrument_id, await _seed_price(instrument_id))
                    subscribed.add(instrument_id)
                    asyncio.create_task(real_price_feed.refresh_instrument_now(instrument_id))
            elif msg_type == "unsubscribe":
                for instrument_id in ids & subscribed:
                    tick_engine.unsubscribe(instrument_id)
                    subscribed.discard(instrument_id)

    async def sender() -> None:
        while True:
            message = await queue.get()
            if message["type"] == "tick" and uuid.UUID(message["instrument_id"]) not in subscribed:
                continue
            await websocket.send_json(message)

    try:
        await asyncio.gather(receiver(), sender())
    except WebSocketDisconnect:
        pass
    finally:
        tick_engine.unregister(queue, subscribed)
