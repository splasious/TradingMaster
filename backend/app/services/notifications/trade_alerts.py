"""Telegram alerts for paper strategies' trades (16 Oct): each trade taken,
and each one closed with its P&L, for the strategies named in
Settings.telegram_trade_alert_strategies -- to the private chat and the
group (telegram_group.py). Sent by the native runner after a check that
opened legs or closed a trade (NativeContext.opened_legs / closed_trades),
so the strategies' own code is unchanged. A roll is a close and an entry.

Lines stay within 32 characters: the group's message block is narrower
than the private chat's (see oi_levels.MAX_WIDTH).
"""

import logging
from datetime import datetime, time, timezone

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.time import as_aware_utc
from app.models.paper_trading import PaperNativeTrade
from app.models.strategy import Strategy
from app.services.broker.zerodha_broker import IST
from app.services.notifications.telegram import send_telegram

logger = logging.getLogger(__name__)

EXIT_REASONS = {
    "ma_stop": "MA trailing stop",
    "ma_exit": "MA exit",
    "square_off_1500": "15:00 square-off",
    "pcr_above_1.05": "PCR above 1.05",
    "pcr_below_0.95": "PCR below 0.95",
    "carried_overnight": "carried overnight",
    "manual": "manual exit",
}


def alerted_strategies() -> set[str]:
    return {name.strip().lower() for name in (get_settings().telegram_trade_alert_strategies or "").split(",") if name.strip()}


def _lots(quantity: float, lot_size: int | None) -> str:
    if lot_size:
        lots = quantity / lot_size
        return f"{lots:g} lot{'s' if lots != 1 else ''}"
    return f"qty {quantity:g}"


def _ist(at: datetime) -> datetime:
    return as_aware_utc(at).astimezone(IST)


def entry_message(strategy: str, legs: list[dict], position: dict | None, at: datetime) -> tuple[str, str]:
    position = position or {}
    view = position.get("bias") or position.get("regime")
    pcr = position.get("pcr_at_entry")
    head = " · ".join(part for part in (view.title() if view else None, f"PCR {pcr:.3f}" if pcr is not None else None) if part)
    lines = [head] if head else []
    lines.append(f"Time {_ist(at):%H:%M:%S}")
    for leg in legs:
        lines.append(f"{'BUY' if leg['side'] == 'buy' else 'SELL'} {leg['symbol']}")
        lines.append(f"  {_lots(leg['quantity'], leg.get('lot_size'))} @ {leg['price']:,.2f}")
    return f"{strategy}: trade taken", "\n".join(lines)


def exit_message(strategy: str, trade: dict, day_net: float, day_trades: int) -> tuple[str, str]:
    pnl, charges = trade["pnl"], trade.get("charges")
    net = pnl - charges if charges is not None else pnl
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
    return f"{strategy}: trade closed · P&L {net:+,.0f}", "\n".join(lines)


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


async def send_trade_alerts(db, deployment, ctx) -> None:
    """The check's closed trades, then its entry, as Telegram alerts -- when
    the strategy is one alerted on. Never raises: an alert is extra."""
    try:
        strategy = await db.get(Strategy, deployment.strategy_id)
        name = (strategy.name if strategy else "").strip()
        if name.lower() not in alerted_strategies():
            return
        for trade in ctx.closed_trades:
            day_net, day_trades = await _day_net(db, deployment.id, trade["closed_at"])
            await send_telegram(*exit_message(name, trade, day_net, day_trades), to_group=True)
        if ctx.opened_legs:
            await send_telegram(*entry_message(name, ctx.opened_legs, ctx.state.get("position"), ctx.now), to_group=True)
    except Exception:
        logger.exception("Trade alerts failed for deployment %s", deployment.id)
