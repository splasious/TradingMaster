"""Bar-by-bar trade simulator (PRD sections 17-18).

The execution convention that makes this a real backtest rather than a
look-ahead-biased toy: a signal computed from bar i's data (available only
once bar i has *closed*) fills at bar **i+1's open**, never bar i's own
close or open. Stop-loss/take-profit are different -- they're standing
orders placed the moment a position opens, so it's legitimate for them to
trigger intrabar against the *current* bar's high/low, including the same
bar the signal fires on.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime

from app.models.market_data import OhlcvCandle
from app.services.backtest.signals import BarSignals


@dataclass
class CostConfig:
    brokerage_pct: float = 0.03  # per trade side, % of notional
    slippage_pct: float = 0.05  # unfavorable fill adjustment, % of price
    tax_pct: float = 0.0  # applied on realized profit only


@dataclass
class PositionSizing:
    type: str = "fixed_quantity"  # "fixed_quantity" | "percent_capital"
    value: float = 1.0


@dataclass
class RiskRules:
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None


@dataclass
class Trade:
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    exit_reason: str


@dataclass
class BacktestOutput:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    final_equity: float = 0.0


def quantity_for(cash: float, price: float, sizing: PositionSizing) -> float:
    if sizing.type == "percent_capital":
        if price <= 0:
            return 0.0
        allocation = cash * (sizing.value / 100)
        # Whole shares only -- real equity/futures trading doesn't fill
        # fractional units, and flooring (never rounding up) guarantees
        # this never allocates more than the requested percentage of cash.
        return float(math.floor(max(0.0, allocation / price)))
    return max(0.0, sizing.value)


def simulate_trades(
    candles: list[OhlcvCandle],
    signals: BarSignals,
    initial_capital: float,
    sizing: PositionSizing,
    risk: RiskRules,
    costs: CostConfig,
) -> BacktestOutput:
    cash = initial_capital
    position: dict | None = None  # {entry_price, quantity, entry_ts}
    pending_entry = False
    pending_exit = False
    trades: list[Trade] = []
    equity_curve: list[tuple[datetime, float]] = []

    def _apply_slippage(price: float, buying: bool) -> float:
        return price * (1 + costs.slippage_pct / 100) if buying else price * (1 - costs.slippage_pct / 100)

    def _brokerage(notional: float) -> float:
        return notional * (costs.brokerage_pct / 100)

    def _close_position(exit_price: float, exit_ts: datetime, reason: str) -> None:
        nonlocal cash, position
        assert position is not None
        fill = _apply_slippage(exit_price, buying=False)
        notional = fill * position["quantity"]
        fee = _brokerage(notional)
        gross_pnl = (fill - position["entry_price"]) * position["quantity"]
        tax = max(0.0, gross_pnl) * (costs.tax_pct / 100)
        net_pnl = gross_pnl - fee - tax
        cash += notional - fee - tax
        pnl_pct = (fill - position["entry_price"]) / position["entry_price"] * 100 if position["entry_price"] else 0.0
        trades.append(
            Trade(
                entry_ts=position["entry_ts"], entry_price=position["entry_price"], exit_ts=exit_ts,
                exit_price=fill, quantity=position["quantity"], pnl=net_pnl, pnl_pct=pnl_pct, exit_reason=reason,
            )
        )
        position = None

    for i, candle in enumerate(candles):
        if pending_entry and position is None:
            fill = _apply_slippage(candle.open, buying=True)
            quantity = quantity_for(cash, fill, sizing)
            if quantity > 0:
                notional = fill * quantity
                fee = _brokerage(notional)
                if notional + fee <= cash:
                    cash -= notional + fee
                    position = {"entry_price": fill, "quantity": quantity, "entry_ts": candle.ts}
            pending_entry = False

        if pending_exit and position is not None:
            _close_position(candle.open, candle.ts, "signal")
            pending_exit = False

        if position is not None:
            stop_price = position["entry_price"] * (1 - risk.stop_loss_pct / 100) if risk.stop_loss_pct else None
            target_price = position["entry_price"] * (1 + risk.take_profit_pct / 100) if risk.take_profit_pct else None
            if stop_price is not None and candle.low <= stop_price:
                _close_position(stop_price, candle.ts, "stop_loss")
            elif target_price is not None and candle.high >= target_price:
                _close_position(target_price, candle.ts, "take_profit")

        mark_price = candle.close
        equity = cash + (position["quantity"] * mark_price if position else 0.0)
        equity_curve.append((candle.ts, equity))

        if position is None and signals.entry[i]:
            pending_entry = True
        elif position is not None and signals.exit[i]:
            pending_exit = True

    if position is not None and candles:
        _close_position(candles[-1].close, candles[-1].ts, "end_of_data")
        equity_curve[-1] = (candles[-1].ts, cash)

    final_equity = equity_curve[-1][1] if equity_curve else initial_capital
    return BacktestOutput(trades=trades, equity_curve=equity_curve, final_equity=final_equity)
