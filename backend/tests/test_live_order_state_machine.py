from app.services.live_trading.order_state_machine import DELTA_STATE_MAP, LiveOrderStatus, can_transition, is_terminal


def test_forward_progress_allowed():
    assert can_transition(LiveOrderStatus.CREATED, LiveOrderStatus.SUBMITTED)
    assert can_transition(LiveOrderStatus.SUBMITTED, LiveOrderStatus.ACKNOWLEDGED)
    assert can_transition(LiveOrderStatus.ACKNOWLEDGED, LiveOrderStatus.OPEN)
    assert can_transition(LiveOrderStatus.OPEN, LiveOrderStatus.FILLED)
    assert can_transition(LiveOrderStatus.OPEN, LiveOrderStatus.PARTIALLY_FILLED)
    assert can_transition(LiveOrderStatus.PARTIALLY_FILLED, LiveOrderStatus.FILLED)


def test_cannot_skip_from_created_to_filled():
    assert not can_transition(LiveOrderStatus.CREATED, LiveOrderStatus.FILLED)


def test_terminal_states_have_no_transitions():
    for terminal in (LiveOrderStatus.FILLED, LiveOrderStatus.CANCELLED, LiveOrderStatus.REJECTED, LiveOrderStatus.EXPIRED):
        assert is_terminal(terminal)
        assert not can_transition(terminal, LiveOrderStatus.OPEN)


def test_non_terminal_states_are_not_terminal():
    for status in (LiveOrderStatus.CREATED, LiveOrderStatus.SUBMITTED, LiveOrderStatus.ACKNOWLEDGED, LiveOrderStatus.OPEN, LiveOrderStatus.PARTIALLY_FILLED):
        assert not is_terminal(status)


def test_delta_state_map_covers_all_documented_states():
    assert set(DELTA_STATE_MAP.keys()) == {"pending", "open", "closed", "cancelled"}
    assert DELTA_STATE_MAP["closed"] == LiveOrderStatus.FILLED
    assert DELTA_STATE_MAP["cancelled"] == LiveOrderStatus.CANCELLED
