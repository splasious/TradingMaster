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

# Zerodha Kite Connect v3's documented order statuses (see
# broker/zerodha_broker.py) -- written to their published API reference,
# not verified against a live account (no Kite Connect subscription
# available while building this).
KITE_STATE_MAP = {
    "PUT ORDER REQ RECEIVED": LiveOrderStatus.SUBMITTED,
    "VALIDATION PENDING": LiveOrderStatus.SUBMITTED,
    "OPEN PENDING": LiveOrderStatus.ACKNOWLEDGED,
    "AMO REQ RECEIVED": LiveOrderStatus.ACKNOWLEDGED,
    "MODIFY PENDING": LiveOrderStatus.OPEN,
    "TRIGGER PENDING": LiveOrderStatus.OPEN,
    "CANCEL PENDING": LiveOrderStatus.OPEN,
    "OPEN": LiveOrderStatus.OPEN,
    "COMPLETE": LiveOrderStatus.FILLED,
    "CANCELLED": LiveOrderStatus.CANCELLED,
    "REJECTED": LiveOrderStatus.REJECTED,
}

# Kotak Neo's order status vocabulary (`ordSt` field in order_report(),
# see broker/kotak_neo_broker.py) -- confirmed via the SDK's own
# order-verification logic (neo_api_client/api/order_api.py), which checks
# for exactly these lowercase values before allowing a cancel.
KOTAK_NEO_STATE_MAP = {
    "rejected": LiveOrderStatus.REJECTED,
    "cancelled": LiveOrderStatus.CANCELLED,
    "open": LiveOrderStatus.OPEN,
    "complete": LiveOrderStatus.FILLED,
    "traded": LiveOrderStatus.FILLED,
}

# HDFC Securities' order statuses -- NOT confirmed against real docs (see
# hdfc_securities_broker.py's module docstring). Both common Indian-
# broker-API casings (Kite's own convention is uppercase, e.g. "COMPLETE";
# Kotak Neo's is lowercase) are listed since which one HDFC actually uses
# is unverified -- verify against a real account before relying on this.
HDFC_STATE_MAP = {
    "open": LiveOrderStatus.OPEN, "OPEN": LiveOrderStatus.OPEN,
    "pending": LiveOrderStatus.SUBMITTED, "PENDING": LiveOrderStatus.SUBMITTED,
    "complete": LiveOrderStatus.FILLED, "COMPLETE": LiveOrderStatus.FILLED,
    "cancelled": LiveOrderStatus.CANCELLED, "CANCELLED": LiveOrderStatus.CANCELLED,
    "rejected": LiveOrderStatus.REJECTED, "REJECTED": LiveOrderStatus.REJECTED,
}

STATE_MAPS: dict[str, dict[str, LiveOrderStatus]] = {
    "delta_exchange": DELTA_STATE_MAP,
    "zerodha_kite": KITE_STATE_MAP,
    "kotak_neo": KOTAK_NEO_STATE_MAP,
    "hdfc_securities": HDFC_STATE_MAP,
}

TERMINAL_STATUSES = {LiveOrderStatus.FILLED, LiveOrderStatus.CANCELLED, LiveOrderStatus.REJECTED, LiveOrderStatus.EXPIRED}


def can_transition(current: LiveOrderStatus, target: LiveOrderStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, set())


def is_terminal(status: LiveOrderStatus) -> bool:
    return status in TERMINAL_STATUSES
