"""Risk engine (PRD section 24): sits between every strategy signal and
order execution, for both paper (Phase 6) and live (Phase 7) trading.
Every rejection carries a specific, auditable reason -- never a bare
"REJECTED" (PRD section 24's explicit requirement).
"""

from dataclasses import dataclass


@dataclass
class RiskDecision:
    approved: bool
    reason: str | None = None


def evaluate_entry(
    *,
    available_cash: float,
    notional: float,
    open_position_count: int,
    max_positions: int | None,
    realized_pnl_today: float,
    initial_capital: float,
    max_daily_loss_pct: float | None,
) -> RiskDecision:
    if notional > available_cash:
        return RiskDecision(
            approved=False,
            reason=f"Insufficient cash: order notional {notional:.2f} exceeds available cash {available_cash:.2f}",
        )

    if max_positions is not None and open_position_count >= max_positions:
        return RiskDecision(
            approved=False,
            reason=f"Max open positions reached ({open_position_count}/{max_positions})",
        )

    if max_daily_loss_pct is not None and initial_capital > 0:
        loss_limit = -abs(max_daily_loss_pct) / 100 * initial_capital
        if realized_pnl_today <= loss_limit:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Daily loss limit breached: realized P&L today {realized_pnl_today:.2f} "
                    f"<= limit {loss_limit:.2f} ({max_daily_loss_pct}% of {initial_capital:.2f})"
                ),
            )

    return RiskDecision(approved=True)


def evaluate_exit() -> RiskDecision:
    # Exits are always allowed -- risk controls gate opening new exposure,
    # never closing existing exposure (PRD section 24: the engine sits
    # between signal and execution to protect against over-exposure, not
    # to trap a position open).
    return RiskDecision(approved=True)
