"""Paper trading engine (PRD section 21):

  Live Market Data -> Strategy -> Signal -> Risk Engine -> Paper Execution -> Paper Portfolio

`evaluate_deployment` is the whole pipeline for one deployment, one tick.
It's a plain async function (not baked into the background loop) so tests
can call it directly and deterministically, the same pattern already used
for backfill/backtest jobs.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import AlertSeverity, AlertType
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import OrderStatus, PaperDeployment, PaperOrder, PaperPortfolio, PaperPosition, PaperTrade
from app.models.strategy import Strategy, StrategyVersion
from app.services.alerts.service import create_alert
from app.services.audit import write_audit_log
from app.services.backtest.engine import PositionSizing, quantity_for
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.ranking import get_universe_ranks
from app.services.risk.engine import evaluate_entry, evaluate_exit
from app.services.strategy.rules import evaluate_rule_node
from app.services.strategy.sandbox import run_python_strategy

LOOKBACK_BARS = 60


@dataclass
class EvaluationOutcome:
    action: str  # "entered" | "exited" | "rejected" | "hold" | "error" | "skipped"
    signal: str | None = None
    price: float | None = None
    reason: str | None = None


def _bar_dict(candle: OhlcvCandle | None, synthetic_close: float) -> dict:
    if candle is None:
        return {"open": synthetic_close, "high": synthetic_close, "low": synthetic_close, "close": synthetic_close, "volume": 0.0}
    return {
        "open": candle.close, "high": max(candle.close, synthetic_close), "low": min(candle.close, synthetic_close),
        "close": synthetic_close, "volume": 0.0,
    }


async def evaluate_deployment(db: AsyncSession, deployment: PaperDeployment) -> EvaluationOutcome:
    version = await db.get(StrategyVersion, deployment.strategy_version_id)
    instrument = await db.get(Instrument, deployment.instrument_id)
    portfolio = await db.get(PaperPortfolio, deployment.portfolio_id)
    if version is None or instrument is None or portfolio is None:
        return EvaluationOutcome(action="error", reason="deployment references missing data")

    position_result = await db.execute(select(PaperPosition).where(PaperPosition.deployment_id == deployment.id))
    position = position_result.scalar_one_or_none()

    candles_result = await db.execute(
        select(OhlcvCandle)
        .where(OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == deployment.timeframe)
        .order_by(OhlcvCandle.ts.desc())
        .limit(LOOKBACK_BARS)
    )
    candles = list(reversed(candles_result.scalars().all()))

    current_price = tick_engine.get_current_price(instrument.id)
    if current_price is None:
        current_price = candles[-1].close if candles else None
    if current_price is None:
        return EvaluationOutcome(action="skipped", reason="no price data available for this instrument")

    now = datetime.now(timezone.utc)
    synthetic_bar = OhlcvCandle(
        instrument_id=instrument.id, timeframe=deployment.timeframe, ts=now,
        open=candles[-1].close if candles else current_price,
        high=max((candles[-1].close if candles else current_price), current_price),
        low=min((candles[-1].close if candles else current_price), current_price),
        close=current_price, volume=0.0, source="live_paper",
    )
    bars_for_eval = [*candles, synthetic_bar]

    # Standing stop-loss/take-profit checks take priority over the signal,
    # same as the backtest engine's convention.
    if position is not None:
        stop_pct = version.risk_rules.get("stop_loss_pct")
        target_pct = version.risk_rules.get("take_profit_pct")
        stop_price = position.avg_entry_price * (1 - stop_pct / 100) if stop_pct else None
        target_price = position.avg_entry_price * (1 + target_pct / 100) if target_pct else None
        if stop_price is not None and current_price <= stop_price:
            return await _exit_position(db, deployment, portfolio, position, stop_price, now, "stop_loss")
        if target_price is not None and current_price >= target_price:
            return await _exit_position(db, deployment, portfolio, position, target_price, now, "take_profit")

    if version.python_code:
        bars_dicts = [
            {"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume or 0.0} for c in bars_for_eval
        ]
        sandbox_params = dict(version.parameters)
        if len(version.instrument_ids) > 1:
            top_n = int(version.parameters.get("top_n", 10))
            ranks = await get_universe_ranks(
                db, version.id, [uuid.UUID(i) for i in version.instrument_ids], deployment.timeframe, top_n,
            )
            my_rank = ranks.get(instrument.id)
            if my_rank:
                sandbox_params.update(
                    {
                        "rank": float(my_rank["rank"]), "universe_size": float(my_rank["total"]),
                        "in_top_n": my_rank["in_top_n"], "in_bottom_n": my_rank["in_bottom_n"],
                    }
                )
        sandbox_result = await run_python_strategy(version.python_code, bars_dicts, sandbox_params)
        if sandbox_result.error:
            return EvaluationOutcome(action="error", reason=sandbox_result.error)
        signal = sandbox_result.signal
    else:
        entry_met = evaluate_rule_node(bars_for_eval, version.entry_rules)
        exit_met = evaluate_rule_node(bars_for_eval, version.exit_rules)
        signal = "BUY" if entry_met else ("SELL" if exit_met else "HOLD")

    deployment.last_evaluated_at = now

    if signal == "BUY" and position is None:
        return await _try_enter(db, deployment, portfolio, version, current_price, now)
    if signal == "SELL" and position is not None:
        return await _exit_position(db, deployment, portfolio, position, current_price, now, "signal")

    await db.commit()
    return EvaluationOutcome(action="hold", signal=signal, price=current_price)


async def exit_deployment_now(db: AsyncSession, deployment: PaperDeployment) -> EvaluationOutcome:
    """Manual exit -- close whatever position this deployment currently
    holds at the best available price, regardless of what the strategy's
    own signal says. Same price-resolution fallback as evaluate_deployment
    (real tick, else last stored candle close)."""
    portfolio = await db.get(PaperPortfolio, deployment.portfolio_id)
    instrument = await db.get(Instrument, deployment.instrument_id)
    if portfolio is None or instrument is None:
        return EvaluationOutcome(action="error", reason="deployment references missing data")

    position_result = await db.execute(select(PaperPosition).where(PaperPosition.deployment_id == deployment.id))
    position = position_result.scalar_one_or_none()
    if position is None:
        return EvaluationOutcome(action="error", reason="no open position to exit")

    current_price = tick_engine.get_current_price(instrument.id)
    if current_price is None:
        candles_result = await db.execute(
            select(OhlcvCandle)
            .where(OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == deployment.timeframe)
            .order_by(OhlcvCandle.ts.desc())
            .limit(1)
        )
        latest = candles_result.scalar_one_or_none()
        current_price = latest.close if latest else None
    if current_price is None:
        return EvaluationOutcome(action="skipped", reason="no price data available for this instrument")

    return await _exit_position(db, deployment, portfolio, position, current_price, datetime.now(timezone.utc), "manual")


async def _try_enter(
    db: AsyncSession, deployment: PaperDeployment, portfolio: PaperPortfolio, version: StrategyVersion,
    price: float, now: datetime,
) -> EvaluationOutcome:
    sizing = PositionSizing(**version.position_sizing)
    quantity = quantity_for(portfolio.cash, price, sizing)
    if quantity <= 0:
        db.add(PaperOrder(deployment_id=deployment.id, side="buy", quantity=0, price=price, status=OrderStatus.REJECTED.value, reason="Position sizing produced zero quantity"))
        await db.commit()
        return EvaluationOutcome(action="rejected", signal="BUY", price=price, reason="zero quantity")

    notional = quantity * price
    open_position_count = (
        await db.execute(select(PaperPosition).join(PaperDeployment).where(PaperDeployment.portfolio_id == portfolio.id))
    ).scalars().all()
    today = now.date()
    todays_trades = (
        await db.execute(
            select(PaperTrade)
            .join(PaperDeployment)
            .where(PaperDeployment.portfolio_id == portfolio.id, PaperTrade.exit_ts >= datetime(today.year, today.month, today.day, tzinfo=timezone.utc))
        )
    ).scalars().all()
    realized_pnl_today = sum(t.pnl for t in todays_trades)

    decision = evaluate_entry(
        available_cash=portfolio.cash, notional=notional, open_position_count=len(open_position_count),
        max_positions=version.risk_rules.get("max_positions"), realized_pnl_today=realized_pnl_today,
        initial_capital=portfolio.initial_capital, max_daily_loss_pct=version.risk_rules.get("max_daily_loss_pct"),
    )

    if not decision.approved:
        db.add(PaperOrder(deployment_id=deployment.id, side="buy", quantity=quantity, price=price, status=OrderStatus.REJECTED.value, reason=decision.reason))
        await write_audit_log(
            db, user_id=portfolio.user_id, action="PAPER_ORDER_REJECTED", object_type="paper_deployment", object_id=str(deployment.id),
            new_value={"reason": decision.reason},
        )
        await create_alert(
            db, user_id=portfolio.user_id, alert_type=AlertType.ORDER_REJECTED.value, severity=AlertSeverity.WARNING,
            title="Paper order rejected", message=decision.reason or "Order rejected by risk engine",
            object_type="paper_deployment", object_id=str(deployment.id),
        )
        await db.commit()
        return EvaluationOutcome(action="rejected", signal="BUY", price=price, reason=decision.reason)

    portfolio.cash -= notional
    db.add(PaperPosition(deployment_id=deployment.id, quantity=quantity, avg_entry_price=price, opened_at=now))
    db.add(PaperOrder(deployment_id=deployment.id, side="buy", quantity=quantity, price=price, status=OrderStatus.FILLED.value))
    strategy = await db.get(Strategy, deployment.strategy_id)
    await create_alert(
        db, user_id=portfolio.user_id, alert_type=AlertType.ORDER_EXECUTED.value, severity=AlertSeverity.INFO,
        title="Paper order filled", message=f"{strategy.name if strategy else 'Strategy'}: bought {quantity} @ {price:.2f}",
        object_type="paper_deployment", object_id=str(deployment.id),
    )
    await db.commit()
    return EvaluationOutcome(action="entered", signal="BUY", price=price)


async def _exit_position(
    db: AsyncSession, deployment: PaperDeployment, portfolio: PaperPortfolio, position: PaperPosition,
    price: float, now: datetime, reason: str,
) -> EvaluationOutcome:
    evaluate_exit()  # exits are always approved; called for symmetry/auditability
    notional = position.quantity * price
    pnl = (price - position.avg_entry_price) * position.quantity
    pnl_pct = (price - position.avg_entry_price) / position.avg_entry_price * 100 if position.avg_entry_price else 0.0

    portfolio.cash += notional
    db.add(
        PaperTrade(
            deployment_id=deployment.id, entry_ts=position.opened_at, entry_price=position.avg_entry_price,
            exit_ts=now, exit_price=price, quantity=position.quantity, pnl=pnl, pnl_pct=pnl_pct, exit_reason=reason,
        )
    )
    db.add(PaperOrder(deployment_id=deployment.id, side="sell", quantity=position.quantity, price=price, status=OrderStatus.FILLED.value, reason=reason))
    await db.delete(position)

    strategy = await db.get(Strategy, deployment.strategy_id)
    strategy_name = strategy.name if strategy else "Strategy"
    if reason == "stop_loss":
        await create_alert(
            db, user_id=portfolio.user_id, alert_type=AlertType.STOP_LOSS_TRIGGERED.value, severity=AlertSeverity.WARNING,
            title="Stop loss triggered", message=f"{strategy_name}: closed at {price:.2f}, P&L {pnl:+.2f}",
            object_type="paper_deployment", object_id=str(deployment.id),
        )
    elif reason == "take_profit":
        await create_alert(
            db, user_id=portfolio.user_id, alert_type=AlertType.TARGET_TRIGGERED.value, severity=AlertSeverity.INFO,
            title="Target triggered", message=f"{strategy_name}: closed at {price:.2f}, P&L {pnl:+.2f}",
            object_type="paper_deployment", object_id=str(deployment.id),
        )
    else:
        await create_alert(
            db, user_id=portfolio.user_id, alert_type=AlertType.ORDER_EXECUTED.value, severity=AlertSeverity.INFO,
            title="Paper position closed", message=f"{strategy_name}: sold at {price:.2f}, P&L {pnl:+.2f}",
            object_type="paper_deployment", object_id=str(deployment.id),
        )

    await db.commit()
    return EvaluationOutcome(action="exited", signal="SELL", price=price, reason=reason)
