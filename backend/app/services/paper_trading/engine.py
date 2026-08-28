"""Paper trading engine (PRD section 21):

  Live Market Data -> Strategy -> Signal -> Risk Engine -> Paper Execution -> Paper Portfolio

`evaluate_deployment` is the whole pipeline for one deployment, one tick.
It's a plain async function (not baked into the background loop) so tests
can call it directly and deterministically, the same pattern already used
for backfill/backtest jobs.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import OrderStatus, PaperDeployment, PaperOrder, PaperPortfolio, PaperPosition, PaperTrade
from app.models.strategy import StrategyVersion
from app.services.audit import write_audit_log
from app.services.backtest.engine import PositionSizing, quantity_for
from app.services.market_data.tick_engine import tick_engine
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
        sandbox_result = await run_python_strategy(version.python_code, bars_dicts, version.parameters)
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
            db, user_id=None, action="PAPER_ORDER_REJECTED", object_type="paper_deployment", object_id=str(deployment.id),
            new_value={"reason": decision.reason},
        )
        await db.commit()
        return EvaluationOutcome(action="rejected", signal="BUY", price=price, reason=decision.reason)

    portfolio.cash -= notional
    db.add(PaperPosition(deployment_id=deployment.id, quantity=quantity, avg_entry_price=price, opened_at=now))
    db.add(PaperOrder(deployment_id=deployment.id, side="buy", quantity=quantity, price=price, status=OrderStatus.FILLED.value))
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
            exit_ts=now, exit_price=price, quantity=position.quantity, pnl=pnl, pnl_pct=pnl_pct,
        )
    )
    db.add(PaperOrder(deployment_id=deployment.id, side="sell", quantity=position.quantity, price=price, status=OrderStatus.FILLED.value, reason=reason))
    await db.delete(position)
    await db.commit()
    return EvaluationOutcome(action="exited", signal="SELL", price=price, reason=reason)
