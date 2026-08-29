"""Strategy deployment state machine (PRD section 25). Every transition
below is wired to a real, earned event somewhere in the codebase -- nothing
here fabricates a "backtested" or "approved" strategy without the
corresponding real thing having actually happened:

  DRAFT              -> BACKTESTED    backtest/runner.py, on a completed backtest
  BACKTESTED         -> OPTIMIZED     (no automatic trigger yet -- optimization
  OPTIMIZED          -> OUT_OF_SAMPLE_TESTED   results don't currently write back
                                        to strategy status; running one is optional,
                                        not a mandatory gate, so BACKTESTED can also
                                        skip straight to PAPER_TRADING below)
  BACKTESTED/
  OPTIMIZED/
  OUT_OF_SAMPLE_TESTED -> PAPER_TRADING   paper_trading endpoint, on deployment start
  PAPER_TRADING       -> VALIDATED    strategies endpoint, explicit owner/admin action
  VALIDATED           -> APPROVED     strategies endpoint, explicit owner/admin action
  APPROVED            -> LIVE         live_trading endpoint, on deployment start
                                       (requires the full PRD section 49 checklist)
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


# Editing a strategy at any stage sends it back to DRAFT -- a code change
# invalidates prior backtest/paper-trading/approval results (PRD section
# 25's pipeline is a real pipeline, not a checklist of independent flags).
_ALLOWED_TRANSITIONS: dict[StrategyStatus, set[StrategyStatus]] = {
    StrategyStatus.DRAFT: {StrategyStatus.BACKTESTED},
    StrategyStatus.BACKTESTED: {StrategyStatus.OPTIMIZED, StrategyStatus.PAPER_TRADING, StrategyStatus.DRAFT},
    StrategyStatus.OPTIMIZED: {StrategyStatus.OUT_OF_SAMPLE_TESTED, StrategyStatus.PAPER_TRADING, StrategyStatus.DRAFT},
    StrategyStatus.OUT_OF_SAMPLE_TESTED: {StrategyStatus.PAPER_TRADING, StrategyStatus.DRAFT},
    StrategyStatus.PAPER_TRADING: {StrategyStatus.VALIDATED, StrategyStatus.DRAFT},
    StrategyStatus.VALIDATED: {StrategyStatus.APPROVED, StrategyStatus.DRAFT},
    StrategyStatus.APPROVED: {StrategyStatus.LIVE, StrategyStatus.DRAFT},
    StrategyStatus.LIVE: {StrategyStatus.DRAFT},
}


def can_transition(current: StrategyStatus, target: StrategyStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, set())
