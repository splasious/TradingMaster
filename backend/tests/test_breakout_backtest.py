"""The 52 Candle Breakout 15 MIN backtest script (scripts/breakout_backtest.py,
28 Oct): every breakout taken at the next candle's open, sold at the next
candle's open after a close below the 10-candle MA, Rs 2 L a trade."""

import importlib.util
import pathlib
from datetime import datetime, timedelta

import pytest

from app.services.broker.zerodha_broker import IST

spec = importlib.util.spec_from_file_location("breakout_backtest", pathlib.Path(__file__).parent.parent / "scripts/breakout_backtest.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)


def sessions(days, per_day=25):
    for day in days:
        t = datetime(2026, 10, day, 9, 15, tzinfo=IST)
        for _ in range(per_day):
            yield t
            t += timedelta(minutes=15)


def bars_from(prices):
    """(start, open, high, low, close) rows into the script's bars."""
    return [{"start": s, "open": o, "high": h, "low": lo, "close": c} for s, o, h, lo, c in prices]


def test_a_breakout_is_bought_at_the_next_open_and_sold_at_the_open_after_a_close_below_the_ma():
    starts = list(sessions((26, 27, 28)))
    rows = [(s, 99, 100, 98, 99) for s in starts[:60]]
    rows.append((starts[60], 99, 104, 99, 103))  # breakout: 3% over the 52-candle high
    rows.append((starts[61], 103.5, 105, 103, 104))  # bought at 103.50
    rows.append((starts[62], 104, 104.5, 100, 100.5))
    rows.append((starts[63], 100, 100.2, 96, 96.5))  # closes under the MA
    rows.append((starts[64], 96, 97, 95, 96.5))  # sold at its open, 96.00
    rows += [(s, 96, 97, 95, 96) for s in starts[65:70]]
    [trade] = bt.simulate("ABB", bars_from(rows))
    assert (trade["entry_price"], trade["exit_price"], trade["quantity"]) == (103.5, 96.0, int(200_000 / 103.5))
    assert trade["entry_at"] == starts[61] and trade["exit_at"] == starts[64] and trade["candles_held"] == 3
    assert trade["breakout_pct"] == pytest.approx(3.0) and trade["gross"] == pytest.approx((96 - 103.5) * trade["quantity"])
    assert trade["charges"] > 0 and trade["net"] == pytest.approx(trade["gross"] - trade["charges"])


def test_the_days_last_candle_is_not_bought_and_an_open_trade_is_marked_at_the_last_close():
    starts = list(sessions((26, 27, 28)))
    rows = [(s, 99, 100, 98, 99) for s in starts[:74]]
    rows.append((starts[74], 99, 104, 99, 103))  # 28 Oct 15:15: the last candle
    assert bt.simulate("ABB", bars_from(rows)) == []
    rows = [(s, 99, 100, 98, 99) for s in starts[:60]] + [(starts[60], 99, 104, 99, 103), (starts[61], 103, 106, 103, 105)]
    [still_open] = bt.simulate("ABB", bars_from(rows))
    assert still_open["exit_at"] is None and still_open["exit_price"] == 105 and still_open["charges"] is None


def test_the_summary_and_csv():
    starts = list(sessions((26, 27, 28)))
    rows = [(s, 99, 100, 98, 99) for s in starts[:60]] + [
        (starts[60], 99, 104, 99, 103), (starts[61], 103.5, 105, 103, 104), (starts[62], 100, 100.2, 96, 96.5), (starts[63], 96, 97, 95, 96)]
    trades = bt.simulate("ABB", bars_from(rows))
    text = bt.summarize(trades, 1, starts[0], starts[-1])
    assert "Trades    1 closed, 0 open now" in text and "Win rate  0.0%" in text and "ABB" in text
    assert bt.trades_csv(trades).decode().splitlines()[1].startswith("ABB,2026-10-28 ")
