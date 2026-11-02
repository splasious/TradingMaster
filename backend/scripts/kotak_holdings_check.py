"""Kotak Neo: why a live run's stocks read as 0 the morning after (2 Nov).

Read-only -- no order is placed. Logs in to the live run's Kotak Neo
account as the app does (oms.get_authenticated_broker), reads its
positions and holdings once, and for each stock the live runs hold works
out the contract key the app compares with (gateway.key) and what the
app's own reading of those replies (native_gateway._kotak_positions and
_holdings) makes of it.

Run inside the backend container (the health check's `kotak_holdings_check`
input pipes it to `python -`). The rows Kotak returned for those stocks --
every field, values included -- go to the PRIVATE Telegram chat only; the
public Actions log gets the field NAMES Kotak uses, counts, and per stock
whether the key and the quantity were found (no names, values or sizes).
"""

import asyncio
import json

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.broker import Broker, BrokerAccount
from app.models.instrument import Instrument
from app.models.live_native import LiveNativeDeployment, LiveNativePosition
from app.services.live_trading import native_gateway as ng
from app.services.live_trading.oms import get_authenticated_broker
from app.services.notifications.telegram import send_telegram


def _fields(rows: list[dict]) -> list[str]:
    return sorted({k for row in rows if isinstance(row, dict) for k in row})


async def main() -> None:
    log, private = [], []
    async with AsyncSessionLocal() as db:
        positions = (
            await db.execute(
                select(LiveNativePosition, LiveNativeDeployment.broker_account_id)
                .join(LiveNativeDeployment, LiveNativeDeployment.id == LiveNativePosition.deployment_id)
                .where(LiveNativeDeployment.status.in_(("active", "paused")))
            )
        ).all()
        accounts = {account_id for _, account_id in positions}
        log.append(f"live positions: {len(positions)} on {len(accounts)} broker account(s)")
        for account_id in accounts:
            account = await db.get(BrokerAccount, account_id)
            broker_row = await db.get(Broker, account.broker_id)
            if broker_row.code != "kotak_neo":
                continue
            broker = await get_authenticated_broker(db, account, for_live_strategies=True)
            gateway = ng.BrokerGateway(broker_row.code, broker)
            raw_positions = await broker.get_positions() or []
            raw_holdings = await broker.get_holdings() or []
            log.append(f"kotak positions rows: {len(raw_positions)}; fields: {', '.join(_fields(raw_positions)) or '-'}")
            log.append(f"kotak holdings rows: {len(raw_holdings)}; fields: {', '.join(_fields(raw_holdings)) or '-'}")
            read_positions = ng._kotak_positions(raw_positions)
            read_holdings = ng._holdings("kotak_neo", raw_holdings)
            for k, (position, _) in enumerate(p for p in positions if p[1] == account_id):
                instrument = await db.get(Instrument, position.instrument_id)
                key = await gateway.key(instrument)
                in_pos, in_hold = read_positions.get(key), read_holdings.get(key)
                matching = [
                    row for row in raw_positions + raw_holdings
                    if isinstance(row, dict) and (instrument.symbol in json.dumps(row) or str(key[1]) in json.dumps(row))
                ]
                found = (in_pos or 0.0) + (in_hold or 0.0)
                log.append(f"  stock {k + 1}: key found in positions {in_pos is not None}, in holdings {in_hold is not None}; "
                           f"the app reads {'the same quantity' if abs(found - position.quantity) < 1e-6 else 'a different quantity'}; "
                           f"Kotak rows naming it: {len(matching)}")
                private.append(f"{instrument.symbol}: live run holds {position.quantity:g}; the app's key {key}; "
                               f"read from positions {in_pos}, from holdings {in_hold}\n"
                               + ("\n".join(json.dumps(row, default=str)[:1500] for row in matching) or "no Kotak row names it"))
    await send_telegram("Kotak Neo holdings check (read-only)", "\n\n".join(private) or "no live positions")
    print("\n".join(log))


if __name__ == "__main__":
    asyncio.run(main())
