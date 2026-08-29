"""Live order lifecycle (PRD section 23). An order is never treated as
"executed" just because place_order() returned -- it moves through this
state machine only as the broker actually confirms each step (PRD Rule 5:
no unconfirmed orders).
"""

import enum


class LiveOrderStatus(str, enum.Enum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


_ALLOWED_TRANSITIONS: dict[LiveOrderStatus, set[LiveOrderStatus]] = {
    LiveOrderStatus.CREATED: {LiveOrderStatus.SUBMITTED, LiveOrderStatus.REJECTED},
    LiveOrderStatus.SUBMITTED: {LiveOrderStatus.ACKNOWLEDGED, LiveOrderStatus.REJECTED},
    LiveOrderStatus.ACKNOWLEDGED: {LiveOrderStatus.OPEN, LiveOrderStatus.FILLED, LiveOrderStatus.REJECTED},
    LiveOrderStatus.OPEN: {
        LiveOrderStatus.PARTIALLY_FILLED, LiveOrderStatus.FILLED, LiveOrderStatus.CANCELLED, LiveOrderStatus.EXPIRED,
    },
    LiveOrderStatus.PARTIALLY_FILLED: {LiveOrderStatus.FILLED, LiveOrderStatus.CANCELLED, LiveOrderStatus.EXPIRED},
    LiveOrderStatus.FILLED: set(),
    LiveOrderStatus.CANCELLED: set(),
    LiveOrderStatus.REJECTED: set(),
    LiveOrderStatus.EXPIRED: set(),
}

# Delta Exchange's 4 documented order states (verified against their API,
# see broker/delta_broker.py), mapped onto the vocabulary above.
DELTA_STATE_MAP = {
    "pending": LiveOrderStatus.ACKNOWLEDGED,
    "open": LiveOrderStatus.OPEN,
    "closed": LiveOrderStatus.FILLED,
    "cancelled": LiveOrderStatus.CANCELLED,
}

TERMINAL_STATUSES = {LiveOrderStatus.FILLED, LiveOrderStatus.CANCELLED, LiveOrderStatus.REJECTED, LiveOrderStatus.EXPIRED}


def can_transition(current: LiveOrderStatus, target: LiveOrderStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, set())


def is_terminal(status: LiveOrderStatus) -> bool:
    return status in TERMINAL_STATUSES
