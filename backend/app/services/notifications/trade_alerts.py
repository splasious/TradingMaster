"""Telegram alerts for paper strategies' trades (16 Oct): each trade taken,
and each one closed with its P&L, for the strategies named in
Settings.telegram_trade_alert_strategies -- to the private chat and the
group (telegram_group.py). Sent by the native runner after a check that
opened legs or closed a trade (NativeContext.opened_legs / closed_trades),
so the strategies' own code is unchanged. A roll is a close and an entry.

Each alert is a chart picture (trade_chart.py) with the text as its caption,
and a colour mark in its first line: 📈 / 📉 / ↔️ for the view taken, 🟢 / 🔴
for a profit or a loss. A chart that can't be drawn (no candles yet this
session, say) leaves the text alert alone, as before.

A calendar's entry (Conti Calendar 15 MIN, 5 Nov) carries its whole setup:
the strike and lots, the net debit, the take-profit and the daily exit, and
where it stands then if volatility holds -- the breakeven range, the
most it can make and lose (options/calendar_payoff.py, as on its card).

Text lines stay within 32 characters: the group's message block is narrower
than the private chat's (see oi_levels.MAX_WIDTH).
"""

import asyncio
import logging
import uuid
from datetime import datetime, time, timezone

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.models.paper_trading import PaperNativeTrade
from app.models.pcr import PcrSnapshot
from app.models.strategy import Strategy
from app.services.broker.zerodha_broker import IST
from app.services.notifications import candle_panel, trade_chart
from app.services.notifications.telegram import send_telegram
from app.services.notifications.trade_chart import ChartSpec, Mark
from app.services.options.calendar_payoff import EXIT_TIME, GRID_POINTS, Payoff, position_payoff
from app.services.strategy.native_strategies import nifty_pcr_futures_hedge as futures_hedge
from app.services.strategy.native_strategies import nifty_pcr_multi_regime as multi_regime

logger = logging.getLogger(__name__)

EXIT_REASONS = {
    "ma_stop": "MA trailing stop",
    "ma_exit": "MA exit",
    "square_off_1500": "15:00 square-off",
    "pcr_above_1.05": "PCR above 1.05",
    "pcr_below_0.95": "PCR below 0.95",
    "carried_overnight": "carried overnight",
    "manual": "manual exit",
    "take_profit": "take-profit hit",
    "stop_loss": "stop-loss hit",
    "time_cutoff_3pm": "15:00 close",
    "time_cutoff_329pm": "15:29 close",
    "weekly_expiry_3pm": "weekly expiry, 15:00",
}
MA_EXITS = {"ma_stop": "MA stop", "ma_exit": "MA exit"}
VIEW_MARKS = {"bullish": "📈", "bearish": "📉", "sideways": "↔️", "calendar": "📅"}
VIEW_ARROWS = {"bullish": ("▲", "good"), "bearish": ("▼", "bad"), "sideways": ("↔", "neutral")}
NIFTY = "NIFTY 50"


def alerted_strategies() -> set[str]:
    return {name.strip().lower() for name in (get_settings().telegram_trade_alert_strategies or "").split(",") if name.strip()}


def _lots(quantity: float, lot_size: int | None) -> str:
    if lot_size:
        lots = quantity / lot_size
        return f"{lots:g} lot{'s' if lots != 1 else ''}"
    return f"qty {quantity:g}"


def _ist(at: datetime) -> datetime:
    return as_aware_utc(at).astimezone(IST)


def _view(position: dict | None) -> str | None:
    return (position or {}).get("bias") or (position or {}).get("regime")


def _net(trade: dict) -> float:
    return trade["pnl"] - trade["charges"] if trade.get("charges") is not None else trade["pnl"]


def entry_message(
    strategy: str, legs: list[dict], position: dict | None, at: datetime, payoff: Payoff | None = None,
) -> tuple[str, str]:
    position = position or {}
    view = _view(position)
    pcr = position.get("pcr_at_entry")
    head = " · ".join(part for part in (view.title() if view else None, f"PCR {pcr:.3f}" if pcr is not None else None) if part)
    lines = [head] if head else []
    lines.append(f"Time {_ist(at):%H:%M:%S}")
    calendar = view == "calendar"
    lots = {_lots(leg["quantity"], leg.get("lot_size")) for leg in legs}
    if calendar and position.get("strike") is not None:
        lines.append(f"Strike {position['strike']:,.0f}" + (f" · {lots.pop()} a leg" if len(lots) == 1 else ""))
    for leg in legs:
        lines.append(f"{'BUY' if leg['side'] == 'buy' else 'SELL'} {leg['symbol']}")
        lines.append(f"  {_lots(leg['quantity'], leg.get('lot_size'))} @ {leg['price']:,.2f}")
    if calendar:
        lines += _calendar_setup(legs, position, payoff)
    return f"{VIEW_MARKS.get(view or '', '🔔')} {strategy}: trade taken", "\n".join(lines)


def _calendar_setup(legs: list[dict], position: dict, payoff: Payoff | None) -> list[str]:
    """The plan under a calendar's legs: what it cost, when it closes, and --
    `payoff`, today's EXIT_TIME at volatility as now -- where it breaks even and
    the most it can make and lose."""
    debit = sum(leg["price"] * leg["quantity"] * (1 if leg["side"] == "buy" else -1) for leg in legs)
    lines = [f"Net {'debit' if debit >= 0 else 'credit'} {abs(debit):,.0f}"]
    target = position.get("take_profit")
    if isinstance(target, (int, float)):
        lines.append(f"Target +{target:,.0f} after costs")
    lines.append(f"Exit {EXIT_TIME:%H:%M} if not hit")
    if payoff is None:
        return lines
    lines.append(f"At {EXIT_TIME:%H:%M}, IV as now:")
    if payoff.max_profit < 0:
        lines.append(" No profit at any level")
    else:
        low = f"{payoff.breakeven_low:,.0f}" if payoff.breakeven_low is not None else "below"
        high = f"{payoff.breakeven_high:,.0f}" if payoff.breakeven_high is not None else "above"
        lines.append(f" Breakeven {low}-{high}")
    best = "Max profit" if payoff.max_profit >= 0 else "Best case"
    lines.append(f" {best} {payoff.max_profit:+,.0f} @{payoff.max_profit_at:,.0f}")
    lines.append(f" Max loss {payoff.max_loss:+,.0f} (±{GRID_POINTS:,})")
    if payoff.target is not None and payoff.max_profit < payoff.target:
        lines.append(f" {payoff.target:,.0f} not reached by {EXIT_TIME:%H:%M}")
    return lines


async def _entry_payoff(db, position: dict | None, ctx, spot: float | None) -> Payoff | None:
    """A calendar entry's figures at its daily exit, as its card shows them; None for
    any other entry, or when they can't be worked out (logged)."""
    if not isinstance(position, dict) or position.get("regime") != "calendar":
        return None
    try:
        instruments, prices = {}, {}
        for name, leg in position["legs"].items():
            instrument = await db.get(Instrument, uuid.UUID(leg["instrument_id"]))
            instruments[name] = instrument
            prices[name] = await ctx.get_price(instrument.id) if instrument is not None else None
        return position_payoff(position, instruments, prices, spot if spot is not None else position.get("entry_spot"), ctx.now)
    except Exception:
        logger.exception("The calendar's exit-time figures for its Telegram entry alert weren't worked out")
        return None


def exit_message(strategy: str, trade: dict, day_net: float, day_trades: int) -> tuple[str, str]:
    pnl, charges = trade["pnl"], trade.get("charges")
    net = _net(trade)
    reason = EXIT_REASONS.get(trade["exit_reason"]) or trade["exit_reason"].replace("_", " ")
    lines = [
        f"Exit: {reason}"[:32],
        f"Held {_ist(trade['opened_at']):%H:%M} → {_ist(trade['closed_at']):%H:%M}",
    ]
    for leg in trade["legs"]:
        symbol = leg.get("instrument_symbol") or leg.get("instrument_id", "?")
        lines.append(f"{'SELL' if leg['side'] == 'short' else 'BUY'} {symbol}")
        lines.append(f"  {_lots(leg['quantity'], leg.get('lot_size'))} {leg['entry_price']:,.2f} → {leg['exit_price']:,.2f}")
    lines.append(f"Gross P&L {pnl:>+14,.2f}")
    if charges is not None:
        lines.append(f"Charges   {-charges:>+14,.2f}")
        lines.append(f"Net P&L   {net:>+14,.2f}")
    lines.append(f"Today net {day_net:>+14,.2f} ({day_trades})")
    return f"{'🟢' if net >= 0 else '🔴'} {strategy}: trade closed · P&L {net:+,.0f}", "\n".join(lines)


# --- the chart ---------------------------------------------------------------


def pcr_levels(strategy: str, view: str | None, position: dict | None) -> list[tuple[float, str]]:
    """The PCR levels drawn for this trade: its strategy's entry and exit for
    the view taken. An exit the position recorded itself (pcr_exit_below /
    pcr_exit_above) wins over the built-in's."""
    name = strategy.strip().lower()
    if name.startswith("nifty pcr futures"):
        entry, exit_ = {"bullish": (futures_hedge.BULLISH_ENTRY, futures_hedge.BULLISH_EXIT),
                        "bearish": (futures_hedge.BEARISH_ENTRY, futures_hedge.BEARISH_EXIT)}.get(view or "", (None, None))
        return [(level, f"{level:.2f} {what}") for level, what in ((entry, "entry"), (exit_, "exit")) if level is not None]
    position = position or {}
    exits = [position.get("pcr_exit_below"), position.get("pcr_exit_above")]
    if not any(level is not None for level in exits) and name.startswith("am op"):
        exits = list(multi_regime.pcr_exit_band(view)) if view in ("bullish", "bearish", "sideways") else []
    levels = [(level, f"{level:.2f} exit") for level in exits if level is not None]
    if name.startswith("am op"):
        entry = {"bullish": multi_regime.BULLISH_ENTRY, "bearish": multi_regime.BEARISH_ENTRY}.get(view or "")
        if entry is not None:
            levels.append((entry, f"{entry:.2f} entry"))
    return levels


class Session:
    """What the trade-alert pictures draw: NIFTY 50's last 25 finished
    15-minute candles and the one forming (candle_panel.py), its live price,
    and the PCR records over those candles."""

    async def load(self, db, now: datetime, price_of=None) -> "Session":
        """`price_of`: an async (instrument id) -> price, for the live NIFTY."""
        self.now, self.day = now, _ist(now).date()
        nifty = (await db.execute(select(Instrument).where(Instrument.symbol == NIFTY))).scalars().first()
        if nifty is None:
            raise LookupError("NIFTY 50 isn't in the catalog")
        self.nifty, self.live = nifty, None
        if price_of is not None:
            try:
                self.live = await price_of(nifty.id)
            except Exception:
                logger.exception("NIFTY price for the trade alert chart not read")
        self.recent = await candle_panel.load_recent(db, nifty.id, now, self.live)
        self.candles, self.ma = self.recent.candles, self.recent.ma
        bars = self.recent.bars
        start = bars[0]["ts"] if bars else now
        rows = (
            await db.execute(
                select(PcrSnapshot.ts, PcrSnapshot.pcr)  # one record per mark
                .where(PcrSnapshot.underlying == "NIFTY", PcrSnapshot.ts >= start, PcrSnapshot.ts <= now, PcrSnapshot.pcr.is_not(None))
                .order_by(PcrSnapshot.ts)
            )
        ).all()
        self.pcr = [(as_aware_utc(ts), pcr) for ts, pcr in rows]
        return self

    def last_close_by(self, at: datetime) -> tuple[dict, float | None] | None:
        """The newest candle finished by `at`, and the MA once it closed."""
        return self.recent.last_close_by(at)


def _legs_text(legs: list[dict]) -> str:
    if len(legs) > 3:
        return f"{len(legs)} legs"
    names = []
    for leg in legs:
        side = "long" if leg["side"] == "buy" else "short"
        if leg.get("instrument_type") == "future":
            names.append(f"{side} future")
        elif leg.get("strike") is not None and leg.get("option_type"):
            names.append(f"{side} {leg['strike']:,.0f} {leg['option_type']}")
        else:
            names.append(f"{side} {leg['symbol']}")
    text = " + ".join(names)
    lots = {_lots(leg["quantity"], leg.get("lot_size")) for leg in legs}
    if len(lots) == 1:
        text += f" · {lots.pop()}"
    return text[:1].upper() + text[1:]


def _calendar_text(legs: list[dict], position: dict | None) -> str:
    """"Sell 09 Nov + buy 23 Nov 23,450 CE+PE · 16 lots each"."""
    position = position or {}
    try:
        weekly = datetime.fromisoformat(position["weekly_expiry"])
        monthly = datetime.fromisoformat(position["monthly_expiry"])
    except (KeyError, TypeError, ValueError):
        return _legs_text(legs)
    lots = {_lots(leg["quantity"], leg.get("lot_size")) for leg in legs}
    return (f"Sell {weekly:%d %b} + buy {monthly:%d %b} {position.get('strike', 0):,.0f} CE+PE"
            + (f" · {lots.pop()} each" if len(lots) == 1 else ""))


def _entry_mark(session: Session, view: str | None, position: dict | None, at: datetime) -> Mark:
    position = position or {}
    spot = position.get("spot_at_entry") or position.get("entry_spot")
    if spot is None:
        last = session.last_close_by(at)
        spot = last[0]["close"] if last else session.recent.bars[-1]["close"]
    when = f"{_ist(at):%H:%M}" if _ist(at).date() == session.day else f"{_ist(at):%d %b %H:%M}"
    return Mark(at=at, price=float(spot), label=f"{(view or 'entry').title()} {when}\nNIFTY {spot:,.0f}")


def entry_chart(strategy: str, session: Session, legs: list[dict], position: dict | None, at: datetime) -> ChartSpec:
    view = _view(position)
    arrow, tone = VIEW_ARROWS.get(view or "", ("●", "neutral"))
    pcr = (position or {}).get("pcr_at_entry")
    headline = " · ".join(part for part in (f"{arrow} {(view or 'Entry').title()}", f"PCR {pcr:.3f}" if pcr is not None else None) if part)
    return ChartSpec(
        title=f"{strategy} · trade taken", day=session.day, recent=session.recent, headline=headline,
        tone=tone, subtitle=_calendar_text(legs, position) if view == "calendar" else _legs_text(legs), view=view,
        entry=_entry_mark(session, view, position, at), pcr=session.pcr,
        levels=pcr_levels(strategy, view, position),
    )


def exit_chart(strategy: str, session: Session, trade: dict, position: dict | None, nifty_now: float | None) -> ChartSpec:
    view = _view(position)
    net = _net(trade)
    opened, closed = as_aware_utc(trade["opened_at"]), as_aware_utc(trade["closed_at"])
    held_from = f"{_ist(opened):%H:%M}" if _ist(opened).date() == session.day else f"{_ist(opened):%d %b %H:%M}"
    reason = EXIT_REASONS.get(trade["exit_reason"]) or trade["exit_reason"].replace("_", " ")
    last = session.last_close_by(closed)
    if trade["exit_reason"] in MA_EXITS and last is not None and last[1] is not None:
        candle, ma = last
        side = "above" if view == "bearish" else "below"
        exit_mark = Mark(at=closed, price=candle["close"], label=(
            f"{MA_EXITS[trade['exit_reason']]} {_ist(closed):%H:%M}\n{_ist(candle['ts']):%H:%M} close {candle['close']:,.0f}\n"
            f"{side} MA {ma:,.0f}"
        ))
    else:
        price = nifty_now if nifty_now is not None else (last[0]["close"] if last else session.recent.bars[-1]["close"])
        when = "" if f"{_ist(closed):%H:%M}" in reason else f" {_ist(closed):%H:%M}"  # "15:00 square-off" has it
        exit_mark = Mark(at=closed, price=float(price), label=f"{reason}{when}\nNIFTY {price:,.0f}")
    return ChartSpec(
        title=f"{strategy} · trade closed", day=session.day, recent=session.recent,
        headline=f"{'▲' if net >= 0 else '▼'} Net P&L {net:+,.0f}", tone="good" if net >= 0 else "bad",
        subtitle=f"Held {held_from} → {_ist(closed):%H:%M} · exit: {reason}", view=view,
        entry=_entry_mark(session, view, position, opened) if session.recent.x(opened) >= 0 else None,
        exit=exit_mark, held_from=opened, held_to=closed, held_tone="good" if net >= 0 else "bad", pcr=session.pcr,
        levels=pcr_levels(strategy, view, position),
    )


async def _picture(spec_of) -> bytes | None:
    """The chart's PNG, or None (logged) when it can't be drawn."""
    try:
        spec = spec_of()
        return await asyncio.to_thread(trade_chart.draw, spec)
    except (LookupError, ValueError) as exc:  # nothing to draw yet (no candles this session): expected, no traceback
        logger.warning("Trade alert chart not drawn (%s); the alert goes as text", exc)
    except Exception:
        logger.exception("Trade alert chart not drawn; the alert goes as text")
    return None


# --- sending -------------------------------------------------------------------


async def _day_net(db, deployment_id, at: datetime) -> tuple[float, int]:
    """This card's net P&L (after estimated charges) and trade count for the IST day of `at`."""
    start = datetime.combine(_ist(at).date(), time(0), tzinfo=IST).astimezone(timezone.utc)
    total, count = (
        await db.execute(
            select(func.coalesce(func.sum(PaperNativeTrade.pnl - func.coalesce(PaperNativeTrade.charges, 0.0)), 0.0), func.count())
            .where(PaperNativeTrade.deployment_id == deployment_id, PaperNativeTrade.closed_at >= start)
        )
    ).one()
    return float(total), int(count)


async def send_trade_alerts(db, deployment, ctx, position_before: dict | None = None) -> None:
    """The check's closed trades, then its entry, as Telegram alerts -- when
    the strategy is one alerted on. `position_before`: the position as the
    check found it, which a closed trade was. Never raises: an alert is extra."""
    try:
        strategy = await db.get(Strategy, deployment.strategy_id)
        name = (strategy.name if strategy else "").strip()
        if name.lower() not in alerted_strategies():
            return
        try:
            session = await Session().load(db, ctx.now, ctx.get_price)
        except LookupError as exc:
            logger.warning("Trade alert chart data not loaded (%s); the alert goes as text", exc)
            session = None
        except Exception:
            logger.exception("Trade alert chart data not loaded; the alert goes as text")
            session = None
        nifty_now = session.live if session is not None else None  # where an exit is marked (not an MA exit)
        for trade in ctx.closed_trades:
            day_net, day_trades = await _day_net(db, deployment.id, trade["closed_at"])
            photo = await _picture(lambda: exit_chart(name, session, trade, position_before, nifty_now)) if session else None
            await send_telegram(*exit_message(name, trade, day_net, day_trades), to_group=True, photo=photo)
        if ctx.opened_legs:
            position = ctx.state.get("position")
            photo = await _picture(lambda: entry_chart(name, session, ctx.opened_legs, position, ctx.now)) if session else None
            payoff = await _entry_payoff(db, position, ctx, nifty_now)
            await send_telegram(*entry_message(name, ctx.opened_legs, position, ctx.now, payoff), to_group=True, photo=photo)
    except Exception:
        logger.exception("Trade alerts failed for deployment %s", deployment.id)
