"""Portfolio-style backtest: one strategy, replayed bar-by-bar across a
whole basket of instruments at once, sharing a single capital pool --
the "Amibroker Portfolio Backtester" model, as opposed to BacktestJob's
single-instrument run.

Two things are deliberately kept separate, matching how Amibroker itself
separates a strategy's Buy/Sell signals from its PositionScore:

  - Entry/exit signals: each instrument's own rules, evaluated
    independently against its own history (the exact same
    compute_visual_signals/compute_python_signals used by the
    single-instrument engine -- no basket awareness here).
  - Position score: a *separate* per-instrument ranking (trailing N-bar
    momentum within the basket, same formula paper_trading/ranking.py
    uses for live basket strategies) used only to decide *which* of
    several simultaneous entry signals actually get filled when capital
    or open-position slots are scarce. This keeps the engine's cost at
    O(instruments x bars) -- cheap arithmetic on already-loaded price
    history -- rather than needing a live re-evaluation of the strategy's
    own rules at every bar for every other instrument in the basket,
    which a fully rank-aware Python sandbox strategy would require.

Fill convention matches engine.py exactly: a signal computed from bar i's
close fills at bar i+1's open. Stop-loss/take-profit are standing orders
and may trigger intrabar, same bar the signal that opened them fired.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.backtest.engine import BacktestOutput, CostConfig, RiskRules
from app.services.backtest.engine import Trade as EngineTrade
from app.services.backtest.signals import BarSignals

POSITION_SCORE_LOOKBACK_BARS = 20


@dataclass
class PortfolioSizing:
    position_size_pct: float = 10.0  # % of *current total equity* per new position
    max_open_positions: int = 10


@dataclass
class PortfolioTrade:
    instrument_id: str
    symbol: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime | None
    exit_price: float | None
    quantity: float
    pnl: float
    pnl_pct: float
    bars_held: int
    exit_reason: str  # "signal" | "stop_loss" | "take_profit" | "open"
    status: str  # "closed" | "open"


@dataclass
class PortfolioBacktestOutput:
    trades: list[PortfolioTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    final_equity: float = 0.0


def _position_score(candles: list[OhlcvCandle], idx: int, lookback: int = POSITION_SCORE_LOOKBACK_BARS) -> float:
    """Trailing momentum as of bar `idx`: % change from `lookback` bars ago
    to now. Higher is "stronger" and wins ties for scarce capital/slots --
    same relative-strength interpretation as paper_trading/ranking.py's
    live basket ranking, just computed once per candidate bar here instead
    of live "as of now"."""
    if idx < lookback:
        return float("-inf")  # not enough history yet -- never prioritized over a scored candidate
    prior = candles[idx - lookback].close
    if not prior:
        return float("-inf")
    return (candles[idx].close - prior) / prior * 100


def simulate_portfolio(
    instruments: list[Instrument],
    candles_by_instrument: dict[str, list[OhlcvCandle]],
    signals_by_instrument: dict[str, BarSignals],
    initial_capital: float,
    sizing: PortfolioSizing,
    risk: RiskRules,
    costs: CostConfig,
) -> PortfolioBacktestOutput:
    symbol_by_id = {str(i.id): i.symbol for i in instruments}

    ts_index: dict[str, dict[datetime, int]] = {
        inst_id: {c.ts: i for i, c in enumerate(candles)} for inst_id, candles in candles_by_instrument.items()
    }
    all_ts = sorted({c.ts for candles in candles_by_instrument.values() for c in candles})

    cash = initial_capital
    open_positions: dict[str, dict] = {}  # inst_id -> {entry_price, quantity, entry_ts, entry_idx}
    pending_entries: set[str] = set()
    pending_exits: set[str] = set()
    trades: list[PortfolioTrade] = []
    equity_curve: list[tuple[datetime, float]] = []
    last_close: dict[str, float] = {}

    def _apply_slippage(price: float, buying: bool) -> float:
        return price * (1 + costs.slippage_pct / 100) if buying else price * (1 - costs.slippage_pct / 100)

    def _brokerage(notional: float) -> float:
        return notional * (costs.brokerage_pct / 100)

    def _close_position(inst_id: str, exit_price: float, exit_ts: datetime, reason: str, bar_idx: int) -> None:
        nonlocal cash
        pos = open_positions.pop(inst_id)
        fill = _apply_slippage(exit_price, buying=False)
        notional = fill * pos["quantity"]
        fee = _brokerage(notional)
        gross_pnl = (fill - pos["entry_price"]) * pos["quantity"]
        tax = max(0.0, gross_pnl) * (costs.tax_pct / 100)
        net_pnl = gross_pnl - fee - tax
        cash += notional - fee - tax
        pnl_pct = (fill - pos["entry_price"]) / pos["entry_price"] * 100 if pos["entry_price"] else 0.0
        trades.append(
            PortfolioTrade(
                instrument_id=inst_id, symbol=symbol_by_id[inst_id], entry_ts=pos["entry_ts"],
                entry_price=pos["entry_price"], exit_ts=exit_ts, exit_price=fill, quantity=pos["quantity"],
                pnl=net_pnl, pnl_pct=pnl_pct, bars_held=bar_idx - pos["entry_idx"], exit_reason=reason, status="closed",
            )
        )

    for ts in all_ts:
        # 1. Exits scheduled from the previous bar's signal, filled at this
        # bar's open -- processed before entries so freed capital is
        # available to this same bar's new positions.
        for inst_id in list(pending_exits):
            idx = ts_index[inst_id].get(ts)
            if idx is None or inst_id not in open_positions:
                continue
            candle = candles_by_instrument[inst_id][idx]
            _close_position(inst_id, candle.open, candle.ts, "signal", idx)
        pending_exits.clear()

        # 2. Standing stop-loss/take-profit, intrabar against this bar's
        # high/low -- same convention as the single-instrument engine.
        for inst_id in list(open_positions.keys()):
            idx = ts_index.get(inst_id, {}).get(ts)
            if idx is None:
                continue
            candle = candles_by_instrument[inst_id][idx]
            pos = open_positions[inst_id]
            stop_price = pos["entry_price"] * (1 - risk.stop_loss_pct / 100) if risk.stop_loss_pct else None
            target_price = pos["entry_price"] * (1 + risk.take_profit_pct / 100) if risk.take_profit_pct else None
            if stop_price is not None and candle.low <= stop_price:
                _close_position(inst_id, stop_price, candle.ts, "stop_loss", idx)
            elif target_price is not None and candle.high >= target_price:
                _close_position(inst_id, target_price, candle.ts, "take_profit", idx)

        # 3. Entries scheduled from the previous bar's signal, filled at
        # this bar's open. When more candidates exist than capital/slots
        # allow, the highest position score wins -- Amibroker's
        # PositionScore mechanic.
        candidates = []
        for inst_id in pending_entries:
            if inst_id in open_positions:
                continue
            idx = ts_index.get(inst_id, {}).get(ts)
            if idx is None:
                continue
            candles = candles_by_instrument[inst_id]
            candidates.append((_position_score(candles, idx), inst_id, candles[idx], idx))
        candidates.sort(key=lambda c: c[0], reverse=True)
        pending_entries.clear()

        for _score, inst_id, candle, idx in candidates:
            if len(open_positions) >= sizing.max_open_positions:
                break
            equity_now = cash + sum(
                open_positions[oid]["quantity"] * last_close.get(oid, open_positions[oid]["entry_price"])
                for oid in open_positions
            )
            allocation = min(equity_now * sizing.position_size_pct / 100, cash)
            fill = _apply_slippage(candle.open, buying=True)
            if fill <= 0:
                continue
            quantity = float(math.floor(allocation / fill))
            if quantity <= 0:
                continue
            notional = fill * quantity
            fee = _brokerage(notional)
            if notional + fee > cash:
                continue
            cash -= notional + fee
            open_positions[inst_id] = {"entry_price": fill, "quantity": quantity, "entry_ts": candle.ts, "entry_idx": idx}

        # 4. Evaluate today's close for tomorrow's fills, and mark equity.
        for inst_id, candles in candles_by_instrument.items():
            idx = ts_index[inst_id].get(ts)
            if idx is None:
                continue
            candle = candles[idx]
            last_close[inst_id] = candle.close
            signals = signals_by_instrument[inst_id]
            if inst_id not in open_positions and idx < len(signals.entry) and signals.entry[idx]:
                pending_entries.add(inst_id)
            elif inst_id in open_positions and idx < len(signals.exit) and signals.exit[idx]:
                pending_exits.add(inst_id)

        equity = cash + sum(open_positions[oid]["quantity"] * last_close.get(oid, 0.0) for oid in open_positions)
        equity_curve.append((ts, equity))

    # Still-open positions at the end of the range stay open (unrealized,
    # marked at the instrument's own last close) rather than force-closed
    # -- matching Amibroker's "Open Long" rows.
    for inst_id, pos in open_positions.items():
        mark = last_close.get(inst_id, pos["entry_price"])
        unrealized_pnl = (mark - pos["entry_price"]) * pos["quantity"]
        pnl_pct = (mark - pos["entry_price"]) / pos["entry_price"] * 100 if pos["entry_price"] else 0.0
        last_idx = len(candles_by_instrument[inst_id]) - 1
        trades.append(
            PortfolioTrade(
                instrument_id=inst_id, symbol=symbol_by_id[inst_id], entry_ts=pos["entry_ts"],
                entry_price=pos["entry_price"], exit_ts=None, exit_price=None, quantity=pos["quantity"],
                pnl=unrealized_pnl, pnl_pct=pnl_pct, bars_held=last_idx - pos["entry_idx"], exit_reason="open", status="open",
            )
        )

    trades.sort(key=lambda t: t.entry_ts)
    final_equity = equity_curve[-1][1] if equity_curve else initial_capital
    return PortfolioBacktestOutput(trades=trades, equity_curve=equity_curve, final_equity=final_equity)


def as_metrics_input(output: PortfolioBacktestOutput, initial_capital: float) -> BacktestOutput:
    """compute_metrics (metrics.py) unconditionally computes `exit_ts -
    entry_ts` per trade, which crashes on a still-open portfolio trade's
    `exit_ts=None`. Mark those to the last equity-curve timestamp, at their
    unrealized mark price, so overall metrics fold in open positions'
    unrealized P&L -- consistent with how the equity curve itself already
    counts them -- without touching the shared single-instrument metrics
    code."""
    last_ts = output.equity_curve[-1][0] if output.equity_curve else None
    engine_trades = [
        EngineTrade(
            entry_ts=t.entry_ts, entry_price=t.entry_price,
            exit_ts=t.exit_ts if t.exit_ts is not None else (last_ts or t.entry_ts),
            exit_price=t.exit_price if t.exit_price is not None else t.entry_price + (t.pnl / t.quantity if t.quantity else 0.0),
            quantity=t.quantity, pnl=t.pnl, pnl_pct=t.pnl_pct, exit_reason=t.exit_reason,
        )
        for t in output.trades
    ]
    return BacktestOutput(trades=engine_trades, equity_curve=output.equity_curve, final_equity=output.final_equity)
