"""Bar-by-bar trade simulator (PRD sections 17-18).

The execution convention that makes this a real backtest rather than a
look-ahead-biased toy: a signal computed from bar i's data (available only
once bar i has *closed*) fills at bar **i+1's open**, never bar i's own
close or open. Stop-loss/take-profit are different -- they're standing
orders placed the moment a position opens, so it's legitimate for them to
trigger intrabar against the *current* bar's high/low, including the same
bar the signal fires on.

SHORT/COVER mirror BUY/SELL on the opposite side (open short, close
short), backtest/portfolio-backtest only -- see signals.py's module
docstring for why live/paper trading never acts on them. A short's
economics are the long side's mirror image throughout: slippage direction
flips (opening a short is a sale -- adverse slippage is downward; closing
one is a purchase -- adverse slippage is upward), stop-loss/take-profit
trigger against the opposite side of the bar (a short's stop is a price
*rise*, checked against the high; its target is a price *fall*, checked
against the low), and PnL is (entry - exit) instead of (exit - entry).
Cash accounting reserves the entry notional as collateral at open (the
same simplification the reference "DS RS 60 MIN" strategy this was built
for already used) and returns collateral plus realized PnL at close --
economically equivalent to the long side's "pay to open, receive to
close", just running in the opposite direction.
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
    side: str = "long"  # "long" | "short"


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
    position: dict | None = None  # {entry_price, quantity, entry_ts, side}
    pending_entry = False
    pending_short_entry = False
    pending_exit = False
    pending_cover = False
    trades: list[Trade] = []
    equity_curve: list[tuple[datetime, float]] = []

    def _apply_slippage(price: float, buying: bool) -> float:
        return price * (1 + costs.slippage_pct / 100) if buying else price * (1 - costs.slippage_pct / 100)

    def _brokerage(notional: float) -> float:
        return notional * (costs.brokerage_pct / 100)

    def _close_long(exit_price: float, exit_ts: datetime, reason: str) -> None:
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
                side="long",
            )
        )
        position = None

    def _close_short(exit_price: float, exit_ts: datetime, reason: str) -> None:
        nonlocal cash, position
        assert position is not None
        fill = _apply_slippage(exit_price, buying=True)  # covering = buying back, adverse slippage is upward
        notional = fill * position["quantity"]
        fee = _brokerage(notional)
        gross_pnl = (position["entry_price"] - fill) * position["quantity"]  # profits when price fell
        tax = max(0.0, gross_pnl) * (costs.tax_pct / 100)
        net_pnl = gross_pnl - fee - tax
        entry_notional = position["entry_price"] * position["quantity"]
        cash += entry_notional + net_pnl  # return reserved collateral, plus realized net pnl
        pnl_pct = (position["entry_price"] - fill) / position["entry_price"] * 100 if position["entry_price"] else 0.0
        trades.append(
            Trade(
                entry_ts=position["entry_ts"], entry_price=position["entry_price"], exit_ts=exit_ts,
                exit_price=fill, quantity=position["quantity"], pnl=net_pnl, pnl_pct=pnl_pct, exit_reason=reason,
                side="short",
            )
        )
        position = None

    for i, candle in enumerate(candles):
        if position is None:
            if pending_entry:
                fill = _apply_slippage(candle.open, buying=True)
                quantity = quantity_for(cash, fill, sizing)
                if quantity > 0:
                    notional = fill * quantity
                    fee = _brokerage(notional)
                    if notional + fee <= cash:
                        cash -= notional + fee
                        position = {"entry_price": fill, "quantity": quantity, "entry_ts": candle.ts, "side": "long"}
            elif pending_short_entry:
                fill = _apply_slippage(candle.open, buying=False)  # opening short = selling, adverse slippage is downward
                quantity = quantity_for(cash, fill, sizing)
                if quantity > 0:
                    notional = fill * quantity  # reserved as collateral, mirroring the long side's "pay to open"
                    fee = _brokerage(notional)
                    if notional + fee <= cash:
                        cash -= notional + fee
                        position = {"entry_price": fill, "quantity": quantity, "entry_ts": candle.ts, "side": "short"}
            pending_entry = False
            pending_short_entry = False
        elif position["side"] == "long" and pending_exit:
            _close_long(candle.open, candle.ts, "signal")
            pending_exit = False
        elif position["side"] == "short" and pending_cover:
            _close_short(candle.open, candle.ts, "signal")
            pending_cover = False

        if position is not None:
            entry_price = position["entry_price"]
            if position["side"] == "long":
                stop_price = entry_price * (1 - risk.stop_loss_pct / 100) if risk.stop_loss_pct else None
                target_price = entry_price * (1 + risk.take_profit_pct / 100) if risk.take_profit_pct else None
                if stop_price is not None and candle.low <= stop_price:
                    _close_long(stop_price, candle.ts, "stop_loss")
                elif target_price is not None and candle.high >= target_price:
                    _close_long(target_price, candle.ts, "take_profit")
            else:
                # A short's stop is a price *rise* (checked against the bar's high); its target is a price *fall*
                # (checked against the low) -- the exact mirror of the long side's checks.
                stop_price = entry_price * (1 + risk.stop_loss_pct / 100) if risk.stop_loss_pct else None
                target_price = entry_price * (1 - risk.take_profit_pct / 100) if risk.take_profit_pct else None
                if stop_price is not None and candle.high >= stop_price:
                    _close_short(stop_price, candle.ts, "stop_loss")
                elif target_price is not None and candle.low <= target_price:
                    _close_short(target_price, candle.ts, "take_profit")

        mark_price = candle.close
        if position is None:
            position_value = 0.0
        elif position["side"] == "long":
            position_value = position["quantity"] * mark_price
        else:
            # Unrealized value if covered now: 2x entry (return of collateral, in economic terms) minus the
            # current price -- rises as price falls below entry, exactly mirroring a long's quantity * mark_price.
            position_value = position["quantity"] * (2 * position["entry_price"] - mark_price)
        equity = cash + position_value
        equity_curve.append((candle.ts, equity))

        if position is None:
            if signals.entry[i]:
                pending_entry = True
            elif signals.short_entry is not None and signals.short_entry[i]:
                pending_short_entry = True
        elif position["side"] == "long":
            if signals.exit[i]:
                pending_exit = True
            elif signals.short_entry is not None and signals.short_entry[i]:
                # A strategy that only ever emits BUY/SHORT (no explicit SELL/COVER)
                # gets automatic reversal: a SHORT signal while long closes the long
                # -- the opposite-direction setup firing IS the exit condition, since
                # generate_signal can only report one thing per bar and has no way to
                # know it's currently long. A strategy that wants single-direction
                # control can still emit an explicit SELL instead; this only ever
                # fires for strategies that emit SHORT in the first place.
                pending_exit = True
        elif position["side"] == "short":
            if signals.short_exit is not None and signals.short_exit[i]:
                pending_cover = True
            elif signals.entry[i]:
                # Mirror of the above: a BUY signal while short covers it.
                pending_cover = True

    if position is not None and candles:
        if position["side"] == "long":
            _close_long(candles[-1].close, candles[-1].ts, "end_of_data")
        else:
            _close_short(candles[-1].close, candles[-1].ts, "end_of_data")
        equity_curve[-1] = (candles[-1].ts, cash)

    final_equity = equity_curve[-1][1] if equity_curve else initial_capital
    return BacktestOutput(trades=trades, equity_curve=equity_curve, final_equity=final_equity)
