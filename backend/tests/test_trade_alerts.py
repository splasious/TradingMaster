"""Telegram trade alerts (services/notifications/trade_alerts.py, 16 Oct):
each trade taken and each one closed, with its P&L, for Nifty PCR Futures and
AM OP TRD 15 MIN -- sent by the native runner after the check, to the
private chat and the group, for the administrator's cards only."""

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.models.instrument import Instrument
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.broker.zerodha_broker import IST
from app.services.notifications import trade_alerts
from app.services.notifications.trade_alerts import entry_message, exit_message
from app.services.paper_trading.native_runner import run_native_strategy

WIDTH = 32  # the group's message block (oi_levels.MAX_WIDTH)

CODE = """
import uuid
from datetime import timedelta
from app.models.instrument import Instrument

async def evaluate(ctx):
    option = await ctx.db.get(Instrument, uuid.UUID(ctx.state["option"]))
    if ctx.state.get("open"):
        await ctx.close_leg(option, "buy", 130, 90.0)
        await ctx.record_trade(
            [{"instrument_id": str(option.id), "side": "short", "quantity": 130, "entry_price": 100.0, "exit_price": 90.0}],
            1300.0, 10.0, "ma_exit", ctx.now - timedelta(hours=1), alert=False,
        )
    if ctx.state.get("reopen", True):
        await ctx.open_leg(option, "sell", 130, 100.0)
        ctx.state["position"] = {"regime": "bullish", "pcr_at_entry": 1.234}
    ctx.state["open"] = True
    ctx.note("hold")
"""


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 10, 16, hh, mm, ss, tzinfo=IST).astimezone(timezone.utc)


def lines(message: tuple[str, str]) -> list[str]:
    return message[1].splitlines()


def test_the_messages_fit_the_groups_width():
    taken = entry_message("Nifty PCR Futures", [
        {"symbol": "NIFTY26OCTFUT", "side": "buy", "quantity": 650, "price": 25214.55, "lot_size": 65},
        {"symbol": "NIFTY26O2025300CE", "side": "sell", "quantity": 650, "price": 152.0, "lot_size": 65},
    ], {"bias": "bullish", "pcr_at_entry": 1.283}, at(11, 30, 4))
    assert taken[0] == "Nifty PCR Futures: trade taken"
    assert lines(taken) == ["Bullish · PCR 1.283", "Time 11:30:04", "BUY NIFTY26OCTFUT", "  10 lots @ 25,214.55",
                            "SELL NIFTY26O2025300CE", "  10 lots @ 152.00"]

    closed = exit_message("Nifty PCR Futures", {
        "legs": [
            {"instrument_symbol": "NIFTY26OCTFUT", "side": "long", "quantity": 650, "lot_size": 65, "entry_price": 25214.55, "exit_price": 25245.0},
            {"instrument_symbol": "NIFTY26O2025300CE", "side": "short", "quantity": 650, "lot_size": 65, "entry_price": 152.0, "exit_price": 136.4},
        ],
        "pnl": 29932.5, "charges": 1233.83, "exit_reason": "ma_stop", "opened_at": at(11, 30, 4), "closed_at": at(14, 15, 3),
    }, 128698.67, 3)
    assert closed[0] == "Nifty PCR Futures: trade closed · P&L +28,699"
    assert lines(closed)[:2] == ["Exit: MA trailing stop", "Held 11:30 → 14:15"]
    assert "  10 lots 25,214.55 → 25,245.00" in lines(closed) and "Net P&L       +28,698.67" in lines(closed)
    assert lines(closed)[-1] == "Today net    +128,698.67 (3)"
    for message in (taken, closed):
        assert max(len(line) for line in lines(message)) <= WIDTH


def test_an_unknown_exit_reason_reads_as_words_and_missing_charges_drop_their_lines():
    closed = exit_message("AM OP TRD 15 MIN", {
        "legs": [{"instrument_id": "x", "side": "short", "quantity": 130, "entry_price": 118.4, "exit_price": 131.2}],
        "pnl": -1664.0, "charges": None, "exit_reason": "pcr_regime_change", "opened_at": at(9, 45), "closed_at": at(12, 30),
    }, -1664.0, 1)
    assert lines(closed)[0] == "Exit: pcr regime change" and "  qty 130 118.40 → 131.20" in lines(closed)
    assert not any(line.startswith(("Charges", "Net P&L")) for line in lines(closed))
    assert closed[0].endswith("P&L -1,664")


class Card:
    async def build(self, db, name: str, admin: bool) -> "Card":
        role = (await db.execute(select(Role).where(Role.name == "administrator"))).scalar_one_or_none() if admin else None
        if role is None:
            role = Role(name="administrator" if admin else f"trader_{uuid.uuid4().hex[:6]}", description="x")
            db.add(role)
            await db.flush()
        user = User(email=f"alerts_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Alerts")
        user.user_roles = [UserRole(role=role)]
        db.add(user)
        await db.flush()
        strategy = Strategy(name=name, owner_id=user.id, code_type="native")
        db.add(strategy)
        await db.flush()
        version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                                  python_code=CODE, position_sizing={}, risk_rules={})
        portfolio = PaperPortfolio(user_id=user.id, cash=1_000_000.0, initial_capital=1_000_000.0)
        option = Instrument(exchange="NFO", symbol=f"NIFTY26O20{uuid.uuid4().int % 10**5}CE", name="opt", instrument_type="option",
                            data_source="zerodha_kite", external_ref=f"ref{uuid.uuid4().hex[:8]}", expiry=date(2026, 10, 20),
                            strike=25300.0, option_type="CE", lot_size=65)
        db.add_all([version, portfolio, option])
        await db.flush()
        self.option = option
        self.deployment = PaperNativeDeployment(portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                                status=DeploymentStatus.ACTIVE.value, state={"option": str(option.id)})
        db.add(self.deployment)
        await db.commit()
        return self


@pytest.fixture
def sent(monkeypatch):
    messages: list[tuple[str, str, bool]] = []

    async def send_telegram(subject, body, *, to_group=False):
        messages.append((subject, body, to_group))

    monkeypatch.setattr(trade_alerts, "send_telegram", send_telegram)
    return messages


async def test_the_runner_alerts_a_trade_taken_then_a_close_before_the_next_entry(db_session, sent):
    card = await Card().build(db_session, "AM OP TRD 15 MIN", admin=True)
    await run_native_strategy(db_session, card.deployment)
    [(subject, body, to_group)] = sent
    assert subject == "AM OP TRD 15 MIN: trade taken" and to_group
    assert body.splitlines()[0] == "Bullish · PCR 1.234" and f"SELL {card.option.symbol}" in body and "  2 lots @ 100.00" in body

    sent.clear()
    await run_native_strategy(db_session, card.deployment)  # a roll: closed, then taken again
    assert [s.split(" · ")[0] for s, _, _ in sent] == ["AM OP TRD 15 MIN: trade closed", "AM OP TRD 15 MIN: trade taken"]
    closed = sent[0][1].splitlines()
    assert closed[0] == "Exit: MA exit" and f"SELL {card.option.symbol}" in closed and "  2 lots 100.00 → 90.00" in closed
    net = float(next(line for line in closed if line.startswith("Net P&L")).split()[-1].replace(",", ""))
    assert "Gross P&L      +1,300.00" in closed and 0 < net < 1300 and sent[0][0].endswith(f"P&L {net:+,.0f}")  # after charges
    assert closed[-1].startswith("Today net") and closed[-1].endswith("(1)")


async def test_a_check_that_trades_nothing_sends_nothing(db_session, sent):
    card = await Card().build(db_session, "Nifty PCR Futures", admin=True)
    card.deployment.state = {"option": str(card.option.id), "open": False, "reopen": False}
    await db_session.commit()
    await run_native_strategy(db_session, card.deployment)
    assert sent == []


async def test_other_strategies_and_other_users_cards_get_no_trade_alerts(db_session, sent):
    other = await Card().build(db_session, "RS Rotation 15 MIN", admin=True)
    await run_native_strategy(db_session, other.deployment)
    not_admin = await Card().build(db_session, "Nifty PCR Futures", admin=False)
    await run_native_strategy(db_session, not_admin.deployment)
    assert sent == []


async def test_a_failing_alert_never_fails_the_check(db_session, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(trade_alerts, "send_telegram", broken)
    card = await Card().build(db_session, "Nifty PCR Futures", admin=True)
    outcome = await run_native_strategy(db_session, card.deployment)
    assert outcome.action == "hold"
    await db_session.refresh(card.deployment)
    assert card.deployment.state["open"] is True
