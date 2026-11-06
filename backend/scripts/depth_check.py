"""The 5-level order book of Conti Calendar 15 MIN's open legs (6 Nov): read-only.

One Kite /quote through the app's own Zerodha session for the four
contracts the card holds: each side's five best levels (price, quantity,
orders), how much of the card's quantity the best level alone would fill,
and the average price filling all of it down the book would give -- next
to the best price the strategy uses now. No order is placed.

Run inside the backend container (the health check's `depth_check` input
pipes it to `python -`). Market data only.
"""

import asyncio
import uuid

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.instrument import Instrument
from app.models.paper_trading import PaperNativeDeployment
from app.models.strategy import Strategy
from app.services.options.pcr_snapshot_scheduler import kite_broker


def walk(levels: list[dict], quantity: float) -> tuple[float | None, float]:
    """(average price filling `quantity` down these levels, the quantity they hold)."""
    filled, cost = 0.0, 0.0
    for level in levels:
        take = min(float(level.get("quantity") or 0), quantity - filled)
        if take <= 0 or not level.get("price"):
            continue
        filled += take
        cost += take * float(level["price"])
        if filled >= quantity:
            break
    held = sum(float(level.get("quantity") or 0) for level in levels if level.get("price"))
    return (cost / filled if filled >= quantity else None), held


async def main() -> None:
    async with AsyncSessionLocal() as db:
        card = (await db.execute(
            select(PaperNativeDeployment).join(Strategy, Strategy.id == PaperNativeDeployment.strategy_id)
            .where(func.trim(Strategy.name) == "Conti Calendar 15 MIN", PaperNativeDeployment.status == "active")
        )).scalars().first()
        position = (card.state or {}).get("position") if card else None
        if not position:
            print("no open Conti Calendar position")
            return
        legs = {name: (leg, await db.get(Instrument, uuid.UUID(leg["instrument_id"]))) for name, leg in position["legs"].items()}
        broker = await kite_broker(db)
        if broker is None:
            print("Zerodha isn't logged in")
            return
        keys = {name: f"NFO:{instrument.external_ref}" for name, (_, instrument) in legs.items()}
        quotes = await broker.get_quote_batch(list(keys.values()))
    for name, (leg, instrument) in legs.items():
        depth = (quotes.get(keys[name]) or {}).get("depth") or {}
        bids, asks = depth.get("buy") or [], depth.get("sell") or []
        quantity = float(leg["quantity"])
        closing = asks if leg["side"] == "sell" else bids  # a sold leg is bought back from the asks
        side = "ask (buy back)" if leg["side"] == "sell" else "bid (sell)"
        print(f"{name} {instrument.external_ref} -- {quantity:g} to close against the {side}")
        print("   bids: " + "  ".join(f"{b.get('price', 0):.2f} x{b.get('quantity', 0)} ({b.get('orders', 0)})" for b in bids))
        print("   asks: " + "  ".join(f"{a.get('price', 0):.2f} x{a.get('quantity', 0)} ({a.get('orders', 0)})" for a in asks))
        best = closing[0] if closing else {}
        average, held = walk(closing, quantity)
        print(f"   best {best.get('price', 0):.2f} holds {best.get('quantity', 0)} of {quantity:g}; five levels hold {held:g}; "
              f"filling all {quantity:g} averages {f'{average:.2f}' if average is not None else 'beyond the five levels'}")


if __name__ == "__main__":
    asyncio.run(main())
