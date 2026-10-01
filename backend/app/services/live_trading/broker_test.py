"""The broker test (Settings > Brokers > Test this broker): before any live
strategy may trade through a broker account, you run one real round trip
through it -- with the same code live strategies use (native_gateway.py) --
and every step is checked:

  1. contracts   a sample -- the test stock, a NIFTY option of each type and
                 a NIFTY future from this app's list -- is matched to the
                 broker's own contract list; what each matched to is shown
  2. holdings    what the account holds of the test stock now is read
  3. buy         1 share, intraday (MIS), at the protected limit; it must
                 fill, and its fill price must be read back
  4. position    the account must now show one share more
  5. sell        the same share; it must fill
  6. position    the account must be back where it was

Only all six passing marks the account tested (BrokerAccount.live_verified_at);
anything else leaves it untested and says which step failed and why. If the
buy filled and the sell didn't, the report says so first: one share is
still held, intraday -- the broker squares it off before the close, or sell
it in the broker's app.

It costs one share's spread and two orders' brokerage; it runs only while
NSE is open and before TEST_CUTOFF (MIS orders stop being accepted near the
close), and refuses a stock above MAX_TEST_PRICE.
"""

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broker import BrokerAccount
from app.models.instrument import Instrument
from app.models.live_trading import LiveOrder
from app.services.backfill_platform.coverage import IST
from app.services.live_trading.broker_contracts import ContractListError, ContractNotFound
from app.services.live_trading.native_gateway import Fill
from app.services.market_data.hours import nse_market_open
from app.services.market_data.live_price import live_price

logger = logging.getLogger(__name__)

TEST_CUTOFF = time(15, 0)
MAX_TEST_PRICE = 1000.0
POSITION_CHECKS = 5
POSITION_CHECK_SECONDS = 1.0
_NIFTY = re.compile(r"^NIFTY\d")


class BrokerTestError(Exception):
    """The test can't start (market closed, stock not usable, ...)."""


@dataclass
class TestStep:
    name: str
    ok: bool
    detail: str


@dataclass
class BrokerTestReport:
    passed: bool
    steps: list[TestStep] = field(default_factory=list)
    contracts: list[dict] = field(default_factory=list)
    still_held: str | None = None


def can_test_now(now: datetime) -> str | None:
    """Why the test can't run now, or None."""
    if not nse_market_open(now):
        return "NSE is closed -- run the test while the market is open"
    if now.astimezone(IST).time() >= TEST_CUTOFF:
        return f"too close to the close for an intraday order -- run it before {TEST_CUTOFF:%H:%M}"
    return None


async def _samples(db: AsyncSession, stock: Instrument, now: datetime) -> list[Instrument]:
    today = now.astimezone(IST).date()
    rows = (await db.execute(
        select(Instrument).where(
            Instrument.exchange == "NFO", Instrument.is_active.is_(True), Instrument.expiry >= today,
            Instrument.instrument_type.in_(("option", "future")),
        ).order_by(Instrument.expiry, Instrument.strike)
    )).scalars().all()
    nifty = [r for r in rows if _NIFTY.match(r.symbol)]
    picked: list[Instrument] = [stock]
    for want in (("option", "CE"), ("option", "PE"), ("future", None)):
        of_kind = [r for r in nifty if r.instrument_type == want[0] and (want[1] is None or r.option_type == want[1])]
        if of_kind:
            nearest = [r for r in of_kind if r.expiry == of_kind[0].expiry]
            picked.append(nearest[len(nearest) // 2])  # a middle strike of the nearest expiry
    return picked


async def _log_order(db: AsyncSession, account: BrokerAccount, user_id: uuid.UUID, stock: Instrument, side: str, cid: str,
                     fill: Fill, now: datetime) -> None:
    db.add(LiveOrder(
        instrument_id=stock.id, broker_account_id=account.id, owner_id=user_id, client_order_id=cid, side=side, quantity=1,
        order_type="protected_limit", status=fill.status.value, product="MIS", purpose="broker_test", created_at=now,
        broker_order_id=fill.broker_order_id, filled_quantity=fill.filled_quantity, average_price=fill.average_price,
        limit_price=fill.limit_price, reason=fill.reason, confirmed_at=datetime.now(IST),
    ))
    await db.flush()


async def _position(gateway, key) -> float:
    return float((await gateway.net_positions()).get(key, 0.0))


async def _wait_for(gateway, key, expected: float) -> float:
    seen = None
    for attempt in range(POSITION_CHECKS):
        seen = await _position(gateway, key)
        if abs(seen - expected) < 1e-6:
            return seen
        if attempt < POSITION_CHECKS - 1:
            await asyncio.sleep(POSITION_CHECK_SECONDS)
    return seen


async def run_broker_test(db: AsyncSession, account: BrokerAccount, user_id: uuid.UUID, stock: Instrument, gateway,
                          now: datetime) -> BrokerTestReport:
    report = BrokerTestReport(passed=False)

    def step(name: str, ok: bool, detail: str) -> bool:
        report.steps.append(TestStep(name, ok, detail))
        return ok

    # 1. contracts
    contracts_ok = True
    for instrument in await _samples(db, stock, now):
        try:
            contract = await gateway.contract(instrument)
            report.contracts.append({"ours": instrument.symbol, "broker_symbol": contract.symbol, "broker_id": contract.token,
                                     "lot_size": contract.lot_size, "ok": True})
        except (ContractNotFound, ContractListError) as exc:
            contracts_ok = False
            report.contracts.append({"ours": instrument.symbol, "error": str(exc), "ok": False})
    if not step("Contracts", contracts_ok, f"{sum(c['ok'] for c in report.contracts)} of {len(report.contracts)} matched"):
        return report

    # 2. holdings now
    try:
        key = await gateway.key(stock)
        before = await _position(gateway, key)
    except Exception as exc:
        step("Holdings", False, f"couldn't read the account's positions and holdings: {exc}")
        return report
    step("Holdings", True, f"{before:g} {stock.symbol} held before the test")

    price = await live_price(db, stock, now)
    if not price:
        step("Price", False, f"no live price for {stock.symbol} right now")
        return report

    # 3. buy one share
    cid = f"tmt-{uuid.uuid4().hex[:20]}"
    buy = await gateway.order(stock, "buy", 1, "MIS", cid, price)
    await _log_order(db, account, user_id, stock, "buy", cid, buy, now)
    if buy.filled_quantity < 1:
        step("Buy", False, f"1 {stock.symbol} at up to {buy.limit_price or price:g}: {buy.reason or buy.status.value}")
        return report
    bought_ok = buy.complete and buy.average_price is not None
    step("Buy", bought_ok, f"1 {stock.symbol} filled at {buy.average_price} (limit {buy.limit_price:g})"
         if bought_ok else f"filled, but the fill price wasn't reported ({buy.reason or buy.status.value})")

    # 4. it shows
    seen = await _wait_for(gateway, key, before + 1)
    step("Position", abs(seen - before - 1) < 1e-6, f"the account shows {seen:g} {stock.symbol} (expected {before + 1:g})")

    # 5. sell it back -- whatever happened above, the share is held
    cid = f"tmt-{uuid.uuid4().hex[:20]}"
    sell_price = await live_price(db, stock, datetime.now(IST)) or price
    sell = await gateway.order(stock, "sell", 1, "MIS", cid, sell_price)
    await _log_order(db, account, user_id, stock, "sell", cid, sell, now)
    if not step("Sell", sell.complete and sell.filled_quantity >= 1,
                f"1 {stock.symbol} filled at {sell.average_price} (limit {sell.limit_price:g})" if sell.complete
                else f"{sell.reason or sell.status.value}"):
        report.still_held = (f"1 share of {stock.symbol} is still held intraday (MIS) in this account -- the broker squares it "
                             "off before the close, or sell it in the broker's app")
        return report

    # 6. back where it was
    seen = await _wait_for(gateway, key, before)
    step("Position", abs(seen - before) < 1e-6, f"the account is back to {seen:g} {stock.symbol} (expected {before:g})")

    report.passed = all(s.ok for s in report.steps)
    if report.passed:
        account.live_verified_at = now
    return report
