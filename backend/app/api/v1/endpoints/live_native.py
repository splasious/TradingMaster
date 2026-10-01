"""Live native strategies -- what Settings > Brokers needs before any can run:
the broker test (live_trading/broker_test.py) and the server's public IP,
which every broker must have registered as the static IP API orders come
from (SEBI's retail algo rules, since 1 April 2026)."""

import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.broker import Broker, BrokerAccount, Environment
from app.models.instrument import Instrument
from app.models.user import User
from app.services.audit import write_audit_log
from app.services.broker.registry import supports_live_strategies
from app.services.live_trading import kill_switch
from app.services.live_trading.broker_test import MAX_TEST_PRICE, can_test_now, run_broker_test
from app.services.live_trading.native_scheduler import open_gateway
from app.services.market_data.live_price import live_price

router = APIRouter()


class BrokerTestIn(BaseModel):
    broker_account_id: uuid.UUID
    symbol: str


class BrokerTestStepOut(BaseModel):
    name: str
    ok: bool
    detail: str


class BrokerTestOut(BaseModel):
    passed: bool
    steps: list[BrokerTestStepOut]
    contracts: list[dict]
    still_held: str | None = None
    live_verified_at: datetime | None = None


class ServerIpOut(BaseModel):
    ipv4: str | None
    ipv6: str | None


@router.post("/broker-test", response_model=BrokerTestOut)
async def broker_test(
    payload: BrokerTestIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader")),
) -> BrokerTestOut:
    account = await db.get(BrokerAccount, payload.broker_account_id)
    if account is None or (account.user_id != user.id and "administrator" not in user.role_names):
        raise HTTPException(status_code=404, detail="Broker account not found")
    broker = await db.get(Broker, account.broker_id)
    if account.environment != Environment.LIVE.value:
        raise HTTPException(status_code=400, detail="Only a live broker account can be tested")
    if not supports_live_strategies(broker.code):
        raise HTTPException(status_code=400, detail=f"Live strategies can't trade through {broker.name} yet")
    now = datetime.now(timezone.utc)
    why_not = can_test_now(now)
    if why_not:
        raise HTTPException(status_code=400, detail=why_not)
    if (await kill_switch.get_kill_switch(db)).active:
        raise HTTPException(status_code=400, detail="The kill switch is on -- no live orders")

    symbol = payload.symbol.strip().upper()
    stock = (await db.execute(
        select(Instrument).where(
            Instrument.exchange == "NSE", Instrument.instrument_type == "equity", func.upper(Instrument.symbol) == symbol,
            Instrument.is_active.is_(True),
        )
    )).scalars().first()
    if stock is None:
        raise HTTPException(status_code=400, detail=f"{symbol} isn't in this app's NSE stock list -- pick another")
    price = await live_price(db, stock, now)
    if not price:
        raise HTTPException(status_code=400, detail=f"No live price for {symbol} right now")
    if price > MAX_TEST_PRICE:
        raise HTTPException(status_code=400, detail=f"{symbol} is at {price:,.2f}; pick a stock under {MAX_TEST_PRICE:,.0f} for the test")

    try:
        gateway = await open_gateway(db, account.id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't log in to {broker.name}: {exc}") from exc
    report = await run_broker_test(db, account, user.id, stock, gateway, now)
    await write_audit_log(
        db, user_id=user.id, action="BROKER_TEST", object_type="broker_account", object_id=str(account.id),
        new_value={"passed": report.passed, "stock": stock.symbol, "failed_steps": [s.name for s in report.steps if not s.ok]},
    )
    await db.commit()
    return BrokerTestOut(
        passed=report.passed, steps=[BrokerTestStepOut(**vars(s)) for s in report.steps], contracts=report.contracts,
        still_held=report.still_held, live_verified_at=account.live_verified_at,
    )


_IP_TTL_SECONDS = 600
_ip_cache: tuple[float, ServerIpOut] | None = None


async def _ask(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
        return response.text.strip() or None if response.status_code == 200 else None
    except httpx.HTTPError:
        return None


@router.get("/server-ip", response_model=ServerIpOut)
async def server_ip(_: User = Depends(get_current_user)) -> ServerIpOut:
    """The public IP(s) this server's requests reach the internet from --
    what to register with each broker. api64 answers over IPv6 when the
    server has it: a broker reached over IPv6 sees that address, not the
    IPv4 one."""
    global _ip_cache
    if _ip_cache is not None and time.monotonic() - _ip_cache[0] < _IP_TTL_SECONDS:
        return _ip_cache[1]
    ipv4 = await _ask("https://api.ipify.org")
    other = await _ask("https://api64.ipify.org")
    out = ServerIpOut(ipv4=ipv4, ipv6=other if other and ":" in other else None)
    if ipv4 or out.ipv6:
        _ip_cache = (time.monotonic(), out)
    return out
