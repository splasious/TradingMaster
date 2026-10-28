"""The 52 Candle Breakout 15 MIN backtest script (scripts/breakout_backtest.py,
28 Oct): a breakout bought at the next candle's open, sold at the next
candle's open after a close below the 10-candle MA, Rs 2 L a trade, at
most 10 stocks held -- a candle's breakouts fill the free slots strongest
first, after its exits have freed theirs."""

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
    [trade], passed = bt.backtest({"ABB": bt.prepare(bars_from(rows))})
    assert passed == 0
    assert (trade["entry_price"], trade["exit_price"], trade["quantity"]) == (103.5, 96.0, int(200_000 / 103.5))
    assert trade["entry_at"] == starts[61] and trade["exit_at"] == starts[64] and trade["candles_held"] == 3
    assert trade["breakout_pct"] == pytest.approx(3.0) and trade["gross"] == pytest.approx((96 - 103.5) * trade["quantity"])
    assert trade["charges"] > 0 and trade["net"] == pytest.approx(trade["gross"] - trade["charges"])


def test_the_days_last_candle_is_not_bought_and_an_open_trade_is_marked_at_the_last_close():
    starts = list(sessions((26, 27, 28)))
    rows = [(s, 99, 100, 98, 99) for s in starts[:74]]
    rows.append((starts[74], 99, 104, 99, 103))  # 28 Oct 15:15: the last candle
    assert bt.backtest({"ABB": bt.prepare(bars_from(rows))}) == ([], 0)
    rows = [(s, 99, 100, 98, 99) for s in starts[:60]] + [(starts[60], 99, 104, 99, 103), (starts[61], 103, 106, 103, 105)]
    [still_open], _ = bt.backtest({"ABB": bt.prepare(bars_from(rows))})
    assert still_open["exit_at"] is None and still_open["exit_price"] == 105 and still_open["charges"] is None


def test_the_summary_and_csv():
    starts = list(sessions((26, 27, 28)))
    rows = [(s, 99, 100, 98, 99) for s in starts[:60]] + [
        (starts[60], 99, 104, 99, 103), (starts[61], 103.5, 105, 103, 104), (starts[62], 100, 100.2, 96, 96.5), (starts[63], 96, 97, 95, 96)]
    trades, passed = bt.backtest({"ABB": bt.prepare(bars_from(rows))})
    text = bt.summarize(trades, passed, 1, starts[0], starts[-1])
    assert "Trades    1 closed, 0 open now" in text and "Win rate  0.0%" in text and "ABB" in text
    assert bt.trades_csv(trades).decode().splitlines()[1].startswith("ABB,2026-10-28 ")


def test_at_most_10_held_strongest_breakouts_first_and_an_exit_frees_its_slot_for_the_same_candle():
    starts = list(sessions((26, 27, 28)))
    stocks = {}
    for k in range(12):  # 12 stocks break out on the same candle, 1% .. 12% over the high
        rows = [(s, 99, 100, 98, 99) for s in starts[:60]]
        close = 100 * (1 + (k + 1) / 100)
        rows += [(starts[60], 99, close + 1, 99, close)] + [(s, close, close + 1, close - 1, close) for s in starts[61:70]]
        stocks[f"S{k:02}"] = bt.prepare(bars_from(rows))
    trades, passed = bt.backtest(stocks)
    assert passed == 2 and {t["symbol"] for t in trades} == {f"S{k:02}" for k in range(2, 12)}  # the 10 strongest
    assert all(t["exit_at"] is None for t in trades)

    # One held stock closes under its MA on the same candle another breaks out: that one gets the slot.
    one = [(s, 99, 100, 98, 99) for s in starts[:60]] + [(starts[60], 99, 104, 99, 103), (starts[61], 103.5, 105, 103, 104),
                                                           (starts[62], 100, 100.2, 96, 96.5), (starts[63], 96, 97, 95, 96)]
    two = [(s, 99, 100, 98, 99) for s in starts[:62]] + [(starts[62], 99, 106, 99, 105), (starts[63], 105, 106, 104, 105)]
    trades, passed = bt.backtest({"ONE": bt.prepare(bars_from(one)), "TWO": bt.prepare(bars_from(two))}, max_positions=1)
    assert passed == 0 and [t["symbol"] for t in sorted(trades, key=lambda t: t["entry_at"])] == ["ONE", "TWO"]
