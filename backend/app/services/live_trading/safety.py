"""Live Trading Safety checklist (PRD section 49). Checked server-side on
every live deployment start -- a client-side checkbox is not a safety
control, this function is.
"""

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broker import BrokerAccount, ConnectionStatus
from app.models.strategy import Strategy, StrategyVersion
from app.services.live_trading.kill_switch import get_kill_switch
from app.services.strategy.state_machine import StrategyStatus


@dataclass
class SafetyCheckResult:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


async def check_live_trading_readiness(
    db: AsyncSession, strategy: Strategy, version: StrategyVersion, broker_account: BrokerAccount
) -> SafetyCheckResult:
    checks: dict[str, bool] = {}
    failures: list[str] = []

    await db.refresh(broker_account, attribute_names=["connection"])
    connection = broker_account.connection
    checks["broker_connected"] = connection is not None and connection.status == ConnectionStatus.CONNECTED.value
    if not checks["broker_connected"]:
        failures.append("Broker account is not connected")

    checks["strategy_approved"] = strategy.status == StrategyStatus.APPROVED.value
    if not checks["strategy_approved"]:
        failures.append(f"Strategy status is '{strategy.status}', must be 'approved' (backtest -> paper trade -> validate -> approve)")

    risk = version.risk_rules or {}
    checks["risk_limits_configured"] = bool(risk.get("stop_loss_pct") or risk.get("max_daily_loss_pct") or risk.get("max_positions"))
    if not checks["risk_limits_configured"]:
        failures.append("No risk limits configured (need at least one of stop_loss_pct, max_daily_loss_pct, max_positions)")

    checks["position_sizing_configured"] = bool(version.position_sizing and version.position_sizing.get("value"))
    if not checks["position_sizing_configured"]:
        failures.append("No position sizing / capital allocation configured on this strategy version")

    kill_switch = await get_kill_switch(db)
    checks["kill_switch_inactive"] = not kill_switch.active
    if kill_switch.active:
        failures.append(f"Global kill switch is active: {kill_switch.reason or 'no reason given'}")

    return SafetyCheckResult(passed=all(checks.values()), checks=checks, failures=failures)
