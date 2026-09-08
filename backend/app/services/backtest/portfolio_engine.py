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

SHORT/COVER mirror BUY/SELL on the opposite side, same economics as
engine.py's single-instrument mirror (see that module's docstring for the
full breakdown: slippage direction, stop/target sides, PnL sign, and the
collateral-reservation cash convention). One extra wrinkle here: a long
and a short candidate can both want the same scarce capital/slot on the
same bar, so they compete on one combined, direction-aware position-score
scale -- a short candidate's score is the *negative* of its trailing
momentum (strong downward momentum scores as "strong" for a short, the
same way strong upward momentum scores as "strong" for a long), so the
existing "highest score wins the slot" mechanic picks the most convicted
trade regardless of which side it's on.

breadth_exit_threshold (optional) adds a basket-wide, strategy-agnostic
risk-off switch: at each bar, basket breadth (advancers vs. decliners
among instruments with enough history -- the same trailing-momentum
definition paper_trading/ranking.py's basket_breadth() uses live) is
computed once for the whole basket, exactly like position score. When
the resulting ratio drops below the threshold, every open LONG is force-
closed (exit_reason="breadth_exit") and no new long can open until a
later bar's breadth ratio rises back to/above the threshold -- "stay flat
on longs while the basket is broadly falling". Shorts are untouched by
this switch. This exists specifically because advance_decline_ratio
(the live/paper-trading breadth signal injected into generate_signal's
params -- see signals.py) has no backtest-time equivalent: params are
fixed for a whole backtest run, not bar-varying, so a strategy can't see
live breadth during a backtest. This engine-level switch is how that
same "is the basket broadly bearish right now" question gets answered
*in* a backtest, without threading a bar-varying value through the
sandbox. Off (None) by default -- zero effect on existing behavior.
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
    side: str = "long"  # "long" | "short"


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


def _basket_breadth_ratio(
    candles_by_instrument: dict[str, list[OhlcvCandle]], ts_index: dict[str, dict[datetime, int]], ts: datetime,
) -> float:
    """Advance/decline ratio across the whole basket as of this bar's close
    -- advancers / decliners, using the same trailing-momentum definition
    (score > 0 / < 0) as _position_score. Mirrors basket_breadth() in
    paper_trading/ranking.py exactly, just computed from already-loaded
    backtest candles instead of a live DB query."""
    advancers = 0
    decliners = 0
    for inst_id, candles in candles_by_instrument.items():
        idx = ts_index[inst_id].get(ts)
        # Skip instruments without enough history yet -- _position_score's
        # -inf sentinel for that case would otherwise be miscounted as a
        # decliner (-inf < 0), same exclusion basket_breadth() in
        # paper_trading/ranking.py applies for the live equivalent.
        if idx is None or idx < POSITION_SCORE_LOOKBACK_BARS:
            continue
        score = _position_score(candles, idx)
        if score > 0:
            advancers += 1
        elif score < 0:
            decliners += 1
    if decliners > 0:
        return advancers / decliners
    return float(advancers) if advancers > 0 else 1.0


def _position_value(pos: dict, mark_price: float) -> float:
    """Mark-to-market value of one open position, mirrored by side -- a
    long's value rises with price; a short's "value if covered now" rises
    as price falls below entry (2x entry minus mark, exactly engine.py's
    single-instrument formula)."""
    if pos["side"] == "long":
        return pos["quantity"] * mark_price
    return pos["quantity"] * (2 * pos["entry_price"] - mark_price)


def simulate_portfolio(
    instruments: list[Instrument],
    candles_by_instrument: dict[str, list[OhlcvCandle]],
    signals_by_instrument: dict[str, BarSignals],
    initial_capital: float,
    sizing: PortfolioSizing,
    risk: RiskRules,
    costs: CostConfig,
    breadth_exit_threshold: float | None = None,
) -> PortfolioBacktestOutput:
    symbol_by_id = {str(i.id): i.symbol for i in instruments}
    lot_size_by_id = {str(i.id): i.lot_size for i in instruments}

    ts_index: dict[str, dict[datetime, int]] = {
        inst_id: {c.ts: i for i, c in enumerate(candles)} for inst_id, candles in candles_by_instrument.items()
    }
    all_ts = sorted({c.ts for candles in candles_by_instrument.values() for c in candles})

    cash = initial_capital
    open_positions: dict[str, dict] = {}  # inst_id -> {entry_price, quantity, entry_ts, entry_idx, side}
    pending_entries: set[str] = set()
    pending_short_entries: set[str] = set()
    pending_exits: set[str] = set()
    pending_covers: set[str] = set()
    trades: list[PortfolioTrade] = []
    equity_curve: list[tuple[datetime, float]] = []
    last_close: dict[str, float] = {}
    # Computed from the PREVIOUS bar's close, applied at this bar's open --
    # same no-lookahead convention as every other signal in this engine.
    breadth_is_bearish = False

    def _apply_slippage(price: float, buying: bool) -> float:
        return price * (1 + costs.slippage_pct / 100) if buying else price * (1 - costs.slippage_pct / 100)

    def _brokerage(notional: float) -> float:
        return notional * (costs.brokerage_pct / 100)

    def _close_position(inst_id: str, exit_price: float, exit_ts: datetime, reason: str, bar_idx: int) -> None:
        nonlocal cash
        pos = open_positions.pop(inst_id)
        if pos["side"] == "long":
            fill = _apply_slippage(exit_price, buying=False)
            notional = fill * pos["quantity"]
            fee = _brokerage(notional)
            gross_pnl = (fill - pos["entry_price"]) * pos["quantity"]
            tax = max(0.0, gross_pnl) * (costs.tax_pct / 100)
            net_pnl = gross_pnl - fee - tax
            cash += notional - fee - tax
            pnl_pct = (fill - pos["entry_price"]) / pos["entry_price"] * 100 if pos["entry_price"] else 0.0
        else:
            fill = _apply_slippage(exit_price, buying=True)  # covering = buying back, adverse slippage upward
            notional = fill * pos["quantity"]
            fee = _brokerage(notional)
            gross_pnl = (pos["entry_price"] - fill) * pos["quantity"]  # profits when price fell
            tax = max(0.0, gross_pnl) * (costs.tax_pct / 100)
            net_pnl = gross_pnl - fee - tax
            entry_notional = pos["entry_price"] * pos["quantity"]
            cash += entry_notional + net_pnl  # return reserved collateral, plus realized net pnl
            pnl_pct = (pos["entry_price"] - fill) / pos["entry_price"] * 100 if pos["entry_price"] else 0.0
        trades.append(
            PortfolioTrade(
                instrument_id=inst_id, symbol=symbol_by_id[inst_id], entry_ts=pos["entry_ts"],
                entry_price=pos["entry_price"], exit_ts=exit_ts, exit_price=fill, quantity=pos["quantity"],
                pnl=net_pnl, pnl_pct=pnl_pct, bars_held=bar_idx - pos["entry_idx"], exit_reason=reason, status="closed",
                side=pos["side"],
            )
        )

    for ts in all_ts:
        # 0. Basket-wide breadth risk-off, computed from the previous bar's
        # close (see module docstring). Force-closes every open long --
        # ahead of the strategy's own exit signals, which still get their
        # normal chance to fire this same bar for anything not caught here.
        if breadth_exit_threshold is not None and breadth_is_bearish:
            for inst_id in list(open_positions.keys()):
                if open_positions[inst_id]["side"] != "long":
                    continue
                idx = ts_index.get(inst_id, {}).get(ts)
                if idx is None:
                    continue
                candle = candles_by_instrument[inst_id][idx]
                _close_position(inst_id, candle.open, candle.ts, "breadth_exit", idx)

        # 1. Exits/covers scheduled from the previous bar's signal, filled
        # at this bar's open -- processed before entries so freed capital
        # is available to this same bar's new positions. Side-gated: an
        # exit signal only closes a long, a cover only closes a short.
        for inst_id in list(pending_exits):
            if inst_id not in open_positions or open_positions[inst_id]["side"] != "long":
                continue
            idx = ts_index[inst_id].get(ts)
            if idx is None:
                continue
            candle = candles_by_instrument[inst_id][idx]
            _close_position(inst_id, candle.open, candle.ts, "signal", idx)
        pending_exits.clear()

        for inst_id in list(pending_covers):
            if inst_id not in open_positions or open_positions[inst_id]["side"] != "short":
                continue
            idx = ts_index[inst_id].get(ts)
            if idx is None:
                continue
            candle = candles_by_instrument[inst_id][idx]
            _close_position(inst_id, candle.open, candle.ts, "signal", idx)
        pending_covers.clear()

        # 2. Standing stop-loss/take-profit, intrabar against this bar's
        # high/low -- same convention as the single-instrument engine,
        # mirrored by side (a short's stop is a price rise, its target a
        # price fall -- the opposite of a long's).
        for inst_id in list(open_positions.keys()):
            idx = ts_index.get(inst_id, {}).get(ts)
            if idx is None:
                continue
            candle = candles_by_instrument[inst_id][idx]
            pos = open_positions[inst_id]
            entry_price = pos["entry_price"]
            if pos["side"] == "long":
                stop_price = entry_price * (1 - risk.stop_loss_pct / 100) if risk.stop_loss_pct else None
                target_price = entry_price * (1 + risk.take_profit_pct / 100) if risk.take_profit_pct else None
                if stop_price is not None and candle.low <= stop_price:
                    _close_position(inst_id, stop_price, candle.ts, "stop_loss", idx)
                elif target_price is not None and candle.high >= target_price:
                    _close_position(inst_id, target_price, candle.ts, "take_profit", idx)
            else:
                stop_price = entry_price * (1 + risk.stop_loss_pct / 100) if risk.stop_loss_pct else None
                target_price = entry_price * (1 - risk.take_profit_pct / 100) if risk.take_profit_pct else None
                if stop_price is not None and candle.high >= stop_price:
                    _close_position(inst_id, stop_price, candle.ts, "stop_loss", idx)
                elif target_price is not None and candle.low <= target_price:
                    _close_position(inst_id, target_price, candle.ts, "take_profit", idx)

        # 3. Entries scheduled from the previous bar's signal, filled at
        # this bar's open. Long and short candidates compete on one
        # combined, direction-aware position-score scale -- see module
        # docstring -- for scarce capital/slots. Amibroker's PositionScore
        # mechanic, extended to a mixed long/short candidate pool.
        candidates = []
        # Breadth risk-off also blocks brand-new longs, not just open ones
        # -- "stay flat on longs" means no new entries either while the
        # basket stays broadly bearish.
        if not (breadth_exit_threshold is not None and breadth_is_bearish):
            for inst_id in pending_entries:
                if inst_id in open_positions:
                    continue
                idx = ts_index.get(inst_id, {}).get(ts)
                if idx is None:
                    continue
                candles = candles_by_instrument[inst_id]
                candidates.append((_position_score(candles, idx), "long", inst_id, candles[idx], idx))
        for inst_id in pending_short_entries:
            if inst_id in open_positions:
                continue
            idx = ts_index.get(inst_id, {}).get(ts)
            if idx is None:
                continue
            candles = candles_by_instrument[inst_id]
            candidates.append((-_position_score(candles, idx), "short", inst_id, candles[idx], idx))
        candidates.sort(key=lambda c: c[0], reverse=True)
        pending_entries.clear()
        pending_short_entries.clear()

        for _score, side, inst_id, candle, idx in candidates:
            if len(open_positions) >= sizing.max_open_positions:
                break
            equity_now = cash + sum(
                _position_value(open_positions[oid], last_close.get(oid, open_positions[oid]["entry_price"]))
                for oid in open_positions
            )
            allocation = min(equity_now * sizing.position_size_pct / 100, cash)
            fill = _apply_slippage(candle.open, buying=(side == "long"))
            if fill <= 0:
                continue
            lot_size = lot_size_by_id.get(inst_id)
            if lot_size and lot_size > 0:
                lots = math.floor(allocation / (fill * lot_size))
                quantity = float(lots * lot_size)
            else:
                quantity = float(math.floor(allocation / fill))
            if quantity <= 0:
                continue
            notional = fill * quantity
            fee = _brokerage(notional)
            if notional + fee > cash:
                continue
            cash -= notional + fee
            open_positions[inst_id] = {"entry_price": fill, "quantity": quantity, "entry_ts": candle.ts, "entry_idx": idx, "side": side}

        # 4. Evaluate today's close for tomorrow's fills, and mark equity.
        for inst_id, candles in candles_by_instrument.items():
            idx = ts_index[inst_id].get(ts)
            if idx is None:
                continue
            candle = candles[idx]
            last_close[inst_id] = candle.close
            signals = signals_by_instrument[inst_id]
            if inst_id not in open_positions:
                if idx < len(signals.entry) and signals.entry[idx]:
                    pending_entries.add(inst_id)
                elif signals.short_entry is not None and idx < len(signals.short_entry) and signals.short_entry[idx]:
                    pending_short_entries.add(inst_id)
            else:
                pos_side = open_positions[inst_id]["side"]
                has_short_entry = signals.short_entry is not None and idx < len(signals.short_entry) and signals.short_entry[idx]
                has_short_exit = signals.short_exit is not None and idx < len(signals.short_exit) and signals.short_exit[idx]
                if pos_side == "long":
                    if idx < len(signals.exit) and signals.exit[idx]:
                        pending_exits.add(inst_id)
                    elif has_short_entry:
                        # Automatic reversal for a strategy that only ever emits
                        # BUY/SHORT: the opposite-direction setup firing while
                        # long IS the exit condition -- see engine.py's mirrored
                        # comment for the full rationale.
                        pending_exits.add(inst_id)
                else:  # short
                    if has_short_exit:
                        pending_covers.add(inst_id)
                    elif idx < len(signals.entry) and signals.entry[idx]:
                        pending_covers.add(inst_id)

        if breadth_exit_threshold is not None:
            breadth_is_bearish = _basket_breadth_ratio(candles_by_instrument, ts_index, ts) < breadth_exit_threshold

        equity = cash + sum(_position_value(open_positions[oid], last_close.get(oid, 0.0)) for oid in open_positions)
        equity_curve.append((ts, equity))

    # Still-open positions at the end of the range stay open (unrealized,
    # marked at the instrument's own last close) rather than force-closed
    # -- matching Amibroker's "Open Long"/"Open Short" rows.
    for inst_id, pos in open_positions.items():
        mark = last_close.get(inst_id, pos["entry_price"])
        if pos["side"] == "long":
            unrealized_pnl = (mark - pos["entry_price"]) * pos["quantity"]
            pnl_pct = (mark - pos["entry_price"]) / pos["entry_price"] * 100 if pos["entry_price"] else 0.0
        else:
            unrealized_pnl = (pos["entry_price"] - mark) * pos["quantity"]
            pnl_pct = (pos["entry_price"] - mark) / pos["entry_price"] * 100 if pos["entry_price"] else 0.0
        last_idx = len(candles_by_instrument[inst_id]) - 1
        trades.append(
            PortfolioTrade(
                instrument_id=inst_id, symbol=symbol_by_id[inst_id], entry_ts=pos["entry_ts"],
                entry_price=pos["entry_price"], exit_ts=None, exit_price=None, quantity=pos["quantity"],
                pnl=unrealized_pnl, pnl_pct=pnl_pct, bars_held=last_idx - pos["entry_idx"], exit_reason="open", status="open",
                side=pos["side"],
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
            quantity=t.quantity, pnl=t.pnl, pnl_pct=t.pnl_pct, exit_reason=t.exit_reason, side=t.side,
        )
        for t in output.trades
    ]
    return BacktestOutput(trades=engine_trades, equity_curve=output.equity_curve, final_equity=output.final_equity)
