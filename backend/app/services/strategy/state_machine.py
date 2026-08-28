"""Strategy deployment state machine (PRD section 25). Modeled fully now so
the transition rules exist and are tested, even though only DRAFT is
reachable until the phases that earn the later states (backtesting,
optimization, paper trading, admin approval) are built -- nothing here
fabricates a "backtested" strategy without a real backtest behind it.
"""

import enum


class StrategyStatus(str, enum.Enum):
    DRAFT = "draft"
    BACKTESTED = "backtested"
    OPTIMIZED = "optimized"
    OUT_OF_SAMPLE_TESTED = "out_of_sample_tested"
    PAPER_TRADING = "paper_trading"
    VALIDATED = "validated"
    APPROVED = "approved"
    LIVE = "live"


# Forward progress requires each earlier gate to have actually happened;
# editing a strategy at any stage sends it back to DRAFT (PRD section 25's
# pipeline is a real pipeline -- a code change invalidates prior results).
_ALLOWED_TRANSITIONS: dict[StrategyStatus, set[StrategyStatus]] = {
    StrategyStatus.DRAFT: {StrategyStatus.BACKTESTED},
    StrategyStatus.BACKTESTED: {StrategyStatus.OPTIMIZED, StrategyStatus.DRAFT},
    StrategyStatus.OPTIMIZED: {StrategyStatus.OUT_OF_SAMPLE_TESTED, StrategyStatus.DRAFT},
    StrategyStatus.OUT_OF_SAMPLE_TESTED: {StrategyStatus.PAPER_TRADING, StrategyStatus.DRAFT},
    StrategyStatus.PAPER_TRADING: {StrategyStatus.VALIDATED, StrategyStatus.DRAFT},
    StrategyStatus.VALIDATED: {StrategyStatus.APPROVED, StrategyStatus.DRAFT},
    StrategyStatus.APPROVED: {StrategyStatus.LIVE, StrategyStatus.DRAFT},
    StrategyStatus.LIVE: {StrategyStatus.DRAFT},
}


def can_transition(current: StrategyStatus, target: StrategyStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, set())
