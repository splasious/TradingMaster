"""Telegram trade alerts (services/notifications/trade_alerts.py, 16 Oct):
each trade taken and each one closed, with its P&L, for Nifty PCR Futures and
AM OP TRD 15 MIN -- sent by the native runner after the check, to the
private chat and the group, for the administrator's cards only -- each as a
chart picture (trade_chart.py) with the text as its caption."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperPortfolio
from app.models.pcr import PcrSnapshot
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.broker.zerodha_broker import IST
from app.services.notifications import trade_alerts, trade_chart
from app.services.notifications.trade_alerts import Session, entry_chart, entry_message, exit_chart, exit_message, pcr_levels
from app.services.paper_trading.native_runner import NativeContext, run_native_strategy

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
    assert taken[0] == "📈 Nifty PCR Futures: trade taken"
    assert lines(taken) == ["Bullish · PCR 1.283", "Time 11:30:04", "BUY NIFTY26OCTFUT", "  10 lots @ 25,214.55",
                            "SELL NIFTY26O2025300CE", "  10 lots @ 152.00"]

    closed = exit_message("Nifty PCR Futures", {
        "legs": [
            {"instrument_symbol": "NIFTY26OCTFUT", "side": "long", "quantity": 650, "lot_size": 65, "entry_price": 25214.55, "exit_price": 25245.0},
            {"instrument_symbol": "NIFTY26O2025300CE", "side": "short", "quantity": 650, "lot_size": 65, "entry_price": 152.0, "exit_price": 136.4},
        ],
        "pnl": 29932.5, "charges": 1233.83, "exit_reason": "ma_stop", "opened_at": at(11, 30, 4), "closed_at": at(14, 15, 3),
    }, 128698.67, 3)
    assert closed[0] == "🟢 Nifty PCR Futures: trade closed · P&L +28,699"
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
    assert closed[0] == "🔴 AM OP TRD 15 MIN: trade closed · P&L -1,664"


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

    async def send_telegram(subject, body, *, to_group=False, photo=None):
        messages.append((subject, body, to_group))

    monkeypatch.setattr(trade_alerts, "send_telegram", send_telegram)
    return messages


async def test_the_runner_alerts_a_trade_taken_then_a_close_before_the_next_entry(db_session, sent):
    card = await Card().build(db_session, "AM OP TRD 15 MIN", admin=True)
    await run_native_strategy(db_session, card.deployment)
    [(subject, body, to_group)] = sent
    assert subject == "📈 AM OP TRD 15 MIN: trade taken" and to_group
    assert body.splitlines()[0] == "Bullish · PCR 1.234" and f"SELL {card.option.symbol}" in body and "  2 lots @ 100.00" in body

    sent.clear()
    await run_native_strategy(db_session, card.deployment)  # a roll: closed, then taken again
    assert [s.split(" · ")[0] for s, _, _ in sent] == ["🟢 AM OP TRD 15 MIN: trade closed", "📈 AM OP TRD 15 MIN: trade taken"]
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


# --- the chart picture ----------------------------------------------------------

DAY = date(2026, 10, 16)
CLOSES = [25118, 25104, 25096, 25110, 25131, 25150, 25162, 25178, 25190, 25204, 25221, 25240,
          25262, 25281, 25300, 25316, 25309, 25296, 25288, 25262]  # the 09:15 .. 14:00 candles
PCRS = [1.02, 1.05, 1.07, 1.06, 1.09, 1.12, 1.16, 1.21, 1.25, 1.283, 1.29, 1.31, 1.30, 1.32, 1.31, 1.30, 1.28, 1.26, 1.24, 1.22]


def ist(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=IST)


def utc(at: datetime) -> datetime:
    return at.astimezone(timezone.utc)


async def nifty_day(db, closes: list[float], pcrs: list[float]) -> Instrument:
    """NIFTY 50: 15 Oct's last 8 candles at 25,140, then 16 Oct's from 09:15
    closing at `closes`; a PCR record at each mark from 09:15."""
    nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index", data_source="zerodha_kite",
                       external_ref="NIFTY 50")
    db.add(nifty)
    await db.flush()
    bars = [(ist(13, 30, day=date(2026, 10, 15)) + timedelta(minutes=15 * n), 25140.0, 25140.0) for n in range(8)]
    bars += [(ist(9, 15) + timedelta(minutes=15 * n), o, c) for n, (o, c) in enumerate(zip([25150.0, *closes[:-1]], closes))]
    for ts, o, c in bars:
        db.add(OhlcvCandle(instrument_id=nifty.id, timeframe="15m", ts=utc(ts), open=o, high=max(o, c) + 8, low=min(o, c) - 8,
                           close=c, volume=None, source="test"))
    for n, pcr in enumerate(pcrs):
        mark = utc(ist(9, 15) + timedelta(minutes=15 * n))
        db.add(PcrSnapshot(underlying="NIFTY", ts=mark, session_date=DAY, captured_at=mark, source="live_quote", strike_window=40,
                           expiries=[], pcr=pcr))
    await db.commit()
    return nifty


def typical(bar: dict) -> float:
    return (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4


async def test_the_session_is_the_last_25_finished_candles_with_the_ma_and_one_pcr_per_mark(db_session):
    await nifty_day(db_session, CLOSES, PCRS)
    session = await Session().load(db_session, utc(ist(14, 10)))  # the 14:00 candle still forming; no live price
    assert session.day == DAY and len(session.candles) == 25 and session.recent.forming is None
    assert f"{session.candles[0]['ts'].astimezone(IST):%d %H:%M}" == "15 14:00"  # 15 Oct's last 6, then 16 Oct's 19
    first_today = session.candles[6]
    assert f"{first_today['ts'].astimezone(IST):%d %H:%M}" == "16 09:15"
    assert session.ma[6] == pytest.approx((25140 * 7 + typical(first_today)) / 8)  # 15 Oct's candles fill it at the open
    assert [pcr for _, pcr in session.pcr] == PCRS[:20]  # to 14:00, the mark before `now`
    candle, ma = session.last_close_by(ist(11, 30, 4))
    assert f"{candle['ts'].astimezone(IST):%H:%M}" == "11:15"
    assert ma == pytest.approx(sum(typical(b) for b in session.candles[7:15]) / 8)


async def test_the_forming_candle_takes_the_live_price(db_session):
    await nifty_day(db_session, CLOSES, PCRS)

    async def price_of(instrument_id):
        return 25250.0

    stored = await Session().load(db_session, utc(ist(14, 10)), price_of)  # the 14:00 candle was saved while forming
    forming = stored.recent.forming
    assert f"{forming['ts'].astimezone(IST):%H:%M}" == "14:00" and forming["close"] == 25250.0 and stored.live == 25250.0
    assert forming["open"] == CLOSES[-2] and stored.recent.describe() == "last 25 candles + now"

    built = (await Session().load(db_session, utc(ist(14, 20)), price_of)).recent.forming  # none saved: built at the live price
    assert f"{built['ts'].astimezone(IST):%H:%M}" == "14:15" and (built["open"], built["close"]) == (CLOSES[-1], 25250.0)
    assert (built["low"], built["high"]) == (25250.0, CLOSES[-1])


async def test_the_exit_chart_marks_the_ma_stop_on_the_candle_that_closed_beyond_it(db_session):
    await nifty_day(db_session, CLOSES, PCRS)
    session = await Session().load(db_session, utc(ist(14, 15, 3)))
    trade = {"legs": [], "pnl": 29932.5, "charges": 1233.83, "exit_reason": "ma_stop", "opened_at": utc(ist(11, 30, 4)),
             "closed_at": utc(ist(14, 15, 3))}
    position = {"bias": "bullish", "pcr_at_entry": 1.283, "spot_at_entry": 25190.0}
    spec = exit_chart("Nifty PCR Futures", session, trade, position, nifty_now=25240.0)
    assert spec.headline == "▲ Net P&L +28,699" and spec.tone == "good" and spec.held_tone == "good"
    assert spec.subtitle == "Held 11:30 → 14:15 · exit: MA trailing stop"
    assert spec.exit.label == f"MA stop 14:15\n14:00 close 25,262\nbelow MA {session.ma[-1]:,.0f}" and spec.exit.price == 25262
    assert spec.entry.label == "Bullish 11:30\nNIFTY 25,190" and spec.levels == [(1.10, "1.10 entry"), (0.95, "0.95 exit")]
    png = trade_chart.draw(spec)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20_000


async def test_the_entry_chart_and_a_losing_close_carried_from_an_earlier_day(db_session):
    await nifty_day(db_session, CLOSES[:9], PCRS[:10])
    session = await Session().load(db_session, utc(ist(11, 30, 4)))
    legs = [{"symbol": f"NIFTY26O2025250{kind}", "side": "sell", "quantity": 130, "price": 118.4, "lot_size": 65,
             "instrument_type": "option", "strike": 25250.0, "option_type": kind} for kind in ("CE", "PE")]
    position = {"regime": "sideways", "pcr_at_entry": 1.004, "entry_spot": 25190.0, "pcr_exit_below": 0.75, "pcr_exit_above": 1.25}
    spec = entry_chart("AM OP TRD 15 MIN", session, legs, position, utc(ist(11, 30, 4)))
    assert spec.headline == "↔ Sideways · PCR 1.004" and spec.tone == "neutral" and spec.exit is None
    assert spec.subtitle == "Short 25,250 CE + short 25,250 PE · 2 lots"
    assert spec.levels == [(0.75, "0.75 exit"), (1.25, "1.25 exit")]
    assert trade_chart.draw(spec)[:4] == b"\x89PNG"

    trade = {"legs": [], "pnl": -12000.0, "charges": 345.0, "exit_reason": "manual",
             "opened_at": utc(ist(14, 30, day=date(2026, 10, 15))), "closed_at": utc(ist(11, 30, 4))}
    closed = exit_chart("AM OP TRD 15 MIN", session, trade, {"regime": "bearish"}, nifty_now=25201.5)
    assert closed.headline == "▼ Net P&L -12,345" and closed.tone == "bad"
    assert closed.entry.label == "Bearish 15 Oct 14:30\nNIFTY 25,140"  # 15 Oct's candles are on the chart too
    assert closed.subtitle == "Held 15 Oct 14:30 → 11:30 · exit: manual exit" and closed.exit.price == 25201.5
    assert closed.levels == [(0.80, "0.80 exit"), (0.75, "0.75 entry")]  # the built-in's: none recorded
    assert trade_chart.draw(closed)[:4] == b"\x89PNG"


def test_the_pcr_levels_drawn_are_the_strategys_own():
    assert pcr_levels("Nifty PCR Futures", "bearish", None) == [(0.90, "0.90 entry"), (1.05, "1.05 exit")]
    assert pcr_levels("AM OP TRD 15 MIN", "bullish", None) == [(1.20, "1.20 exit"), (1.25, "1.25 entry")]
    assert pcr_levels("AM OP TRD 15 MIN", "bullish", {"pcr_exit_below": 1.15}) == [(1.15, "1.15 exit"), (1.25, "1.25 entry")]
    assert pcr_levels("Something else", "bullish", None) == []


async def test_early_in_the_day_the_chart_carries_the_previous_session(db_session):
    await nifty_day(db_session, CLOSES[:1], PCRS[:1])
    session = await Session().load(db_session, utc(ist(9, 20)))  # 09:15 still forming
    assert len(session.candles) == 8 and {c["ts"].astimezone(IST).date() for c in session.candles} == {date(2026, 10, 15)}
    assert await trade_alerts._picture(lambda: entry_chart("Nifty PCR Futures", session, [], {"bias": "bullish"}, utc(ist(9, 20))))


async def test_no_candles_at_all_sends_the_text_alone(db_session):
    db_session.add(Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index",
                              data_source="zerodha_kite", external_ref="NIFTY 50"))
    await db_session.commit()
    session = await Session().load(db_session, utc(ist(9, 20)))
    assert session.candles == []
    assert await trade_alerts._picture(lambda: entry_chart("Nifty PCR Futures", session, [], {"bias": "bullish"}, utc(ist(9, 20)))) is None


async def test_the_alerts_carry_the_chart_picture(db_session, monkeypatch):
    await nifty_day(db_session, CLOSES[:9], PCRS[:10])
    calls: list[tuple[str, bytes | None]] = []

    async def send_telegram(subject, body, *, to_group=False, photo=None):
        calls.append((subject, photo))

    monkeypatch.setattr(trade_alerts, "send_telegram", send_telegram)
    card = await Card().build(db_session, "Nifty PCR Futures", admin=True)
    portfolio = await db_session.get(PaperPortfolio, card.deployment.portfolio_id)
    ctx = NativeContext(db=db_session, portfolio=portfolio, deployment=card.deployment, now=utc(ist(11, 30, 4)),
                        state={"position": {"bias": "bullish", "pcr_at_entry": 1.283, "spot_at_entry": 25190.0}})
    ctx.opened_legs = [{"symbol": "NIFTY26OCTFUT", "side": "buy", "quantity": 650, "price": 25214.55, "lot_size": 65,
                        "instrument_type": "future", "strike": None, "option_type": None}]
    await trade_alerts.send_trade_alerts(db_session, card.deployment, ctx, None)
    [(subject, photo)] = calls
    assert subject == "📈 Nifty PCR Futures: trade taken" and photo[:4] == b"\x89PNG"
