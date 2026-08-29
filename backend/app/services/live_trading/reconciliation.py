"""Reconciliation engine (PRD section 28): after a restart, reconnect, or
any interruption, compare what this app thinks is true against what the
broker actually reports. Detection only -- discrepancies are surfaced, never
silently auto-corrected (PRD: "Never silently overwrite state"). Resolving
one is a deliberate follow-up action, not something this function does.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broker import BrokerAccount
from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment, LivePosition
from app.services.market_data.delta_source import DeltaExchangeDataSource

QUANTITY_TOLERANCE = 1e-6


@dataclass
class ReconciliationReport:
    matched: list[dict] = field(default_factory=list)
    local_only: list[dict] = field(default_factory=list)  # we think it's open, broker disagrees
    broker_only: list[dict] = field(default_factory=list)  # broker has it, we don't
    quantity_mismatches: list[dict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.local_only or self.broker_only or self.quantity_mismatches)


async def reconcile_positions(db: AsyncSession, broker_account: BrokerAccount) -> ReconciliationReport:
    from app.services.live_trading.oms import _get_authenticated_broker  # local import avoids a cycle

    broker = await _get_authenticated_broker(db, broker_account)
    broker_positions = {p["product_id"]: p for p in await broker.get_positions()}

    local_result = await db.execute(
        select(LivePosition, LiveDeployment, Instrument)
        .join(LiveDeployment, LivePosition.deployment_id == LiveDeployment.id)
        .join(Instrument, LiveDeployment.instrument_id == Instrument.id)
        .where(LiveDeployment.broker_account_id == broker_account.id)
    )
    rows = local_result.all()

    data_source = DeltaExchangeDataSource()
    report = ReconciliationReport()
    seen_product_ids: set[int] = set()

    for position, deployment, instrument in rows:
        ticker = await data_source.get_ticker(instrument.external_ref)
        product_id = ticker["product_id"]
        seen_product_ids.add(product_id)
        broker_position = broker_positions.get(product_id)

        if broker_position is None or float(broker_position.get("size", 0)) == 0:
            report.local_only.append({
                "deployment_id": str(deployment.id), "instrument": instrument.symbol,
                "local_quantity": position.quantity,
            })
            continue

        broker_size = abs(float(broker_position["size"]))
        if abs(broker_size - position.quantity) > QUANTITY_TOLERANCE:
            report.quantity_mismatches.append({
                "deployment_id": str(deployment.id), "instrument": instrument.symbol,
                "local_quantity": position.quantity, "broker_quantity": broker_size,
            })
        else:
            report.matched.append({"deployment_id": str(deployment.id), "instrument": instrument.symbol, "quantity": position.quantity})

    for product_id, broker_position in broker_positions.items():
        if product_id not in seen_product_ids and float(broker_position.get("size", 0)) != 0:
            report.broker_only.append({
                "product_id": product_id, "product_symbol": broker_position.get("product_symbol"),
                "broker_quantity": broker_position.get("size"),
            })

    return report
