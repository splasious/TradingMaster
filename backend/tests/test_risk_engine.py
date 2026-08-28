from app.services.risk.engine import evaluate_entry, evaluate_exit


def test_entry_approved_when_all_checks_pass():
    decision = evaluate_entry(
        available_cash=10000, notional=5000, open_position_count=0, max_positions=3,
        realized_pnl_today=0, initial_capital=100000, max_daily_loss_pct=5,
    )
    assert decision.approved is True
    assert decision.reason is None


def test_entry_rejected_for_insufficient_cash():
    decision = evaluate_entry(
        available_cash=1000, notional=5000, open_position_count=0, max_positions=None,
        realized_pnl_today=0, initial_capital=100000, max_daily_loss_pct=None,
    )
    assert decision.approved is False
    assert "Insufficient cash" in decision.reason


def test_entry_rejected_at_max_positions():
    decision = evaluate_entry(
        available_cash=10000, notional=1000, open_position_count=3, max_positions=3,
        realized_pnl_today=0, initial_capital=100000, max_daily_loss_pct=None,
    )
    assert decision.approved is False
    assert "Max open positions" in decision.reason


def test_entry_allowed_below_max_positions():
    decision = evaluate_entry(
        available_cash=10000, notional=1000, open_position_count=2, max_positions=3,
        realized_pnl_today=0, initial_capital=100000, max_daily_loss_pct=None,
    )
    assert decision.approved is True


def test_entry_rejected_when_daily_loss_limit_breached():
    decision = evaluate_entry(
        available_cash=10000, notional=1000, open_position_count=0, max_positions=None,
        realized_pnl_today=-6000, initial_capital=100000, max_daily_loss_pct=5,  # limit = -5000
    )
    assert decision.approved is False
    assert "Daily loss limit" in decision.reason


def test_entry_allowed_when_daily_loss_within_limit():
    decision = evaluate_entry(
        available_cash=10000, notional=1000, open_position_count=0, max_positions=None,
        realized_pnl_today=-2000, initial_capital=100000, max_daily_loss_pct=5,  # limit = -5000
    )
    assert decision.approved is True


def test_entry_ignores_unset_limits():
    decision = evaluate_entry(
        available_cash=10000, notional=1000, open_position_count=999, max_positions=None,
        realized_pnl_today=-999999, initial_capital=100000, max_daily_loss_pct=None,
    )
    assert decision.approved is True


def test_exit_always_approved():
    assert evaluate_exit().approved is True
