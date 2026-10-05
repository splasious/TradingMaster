"""Nifty RS Rotation (weekly) -- agreed 6 Oct: buys when started, readjusts
Fridays at 15:00 to equal weights (a 1-point band), shifts to the trading
day before a Friday holiday, ranks with this week closing at the price at
the moment, and never reads a later candle in a backtest."""

import math
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import PaperNativeDeployment, PaperPortfolio
from app.services.backfill_platform.coverage import IST
from app.services.backtest.native_runner import BacktestNativeContext
from app.services.strategy.native_strategies import nifty_rs_rotation as rs

STOCKS = ["UPA", "UPB", "FLAT", "DOWN"]
FIRST_DAY = date(2025, 1, 6)


def _ist(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def _day_ts(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=IST).astimezone(timezone.utc)  # Kite's daily candle: 00:00 IST


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(rs, "STOCK_UNIVERSE", STOCKS)
    monkeypatch.setattr(rs, "TOP_N", 2)
    monkeypatch.setattr(rs, "POSITION_SIZE_PCT", 50.0)
    monkeypatch.setattr(rs, "REWEIGHT_BAND_PCT", 1.0)


class Market:
    """NIFTY 50 and four stocks with daily candles from FIRST_DAY; `trend`
    gives each one's daily growth, changeable from a date on."""

    def __init__(self, db):
        self.db = db

    async def build(self, until: date, trend: dict, skip: set[date] = frozenset(), switch: tuple[date, dict] | None = None):
        db = self.db
        self.nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index", data_source="zerodha_kite",
                                external_ref="NIFTY 50")
        self.stocks = {s: Instrument(exchange="NSE", symbol=s, name=s, instrument_type="equity", data_source="zerodha_kite", external_ref=s)
                       for s in STOCKS}
        db.add_all([self.nifty, *self.stocks.values()])
        await db.flush()
        names = {self.nifty.id: "NIFTY 50", **{i.id: s for s, i in self.stocks.items()}}
        price = {i: 100.0 for i in names}
        d = FIRST_DAY
        while d <= until:
            if d.weekday() < 5 and d not in skip:
                rates = switch[1] if switch and d >= switch[0] else trend
                for instrument_id, name in names.items():
                    open_ = price[instrument_id]
                    price[instrument_id] = open_ * math.exp(rates.get(name, 0.0))
                    db.add(OhlcvCandle(instrument_id=instrument_id, timeframe="1d", ts=_day_ts(d), open=open_, high=open_, low=open_,
                                       close=price[instrument_id], volume=1, source="test"))
            d += timedelta(days=1)
        self.portfolio = PaperPortfolio(user_id=None, name="t", cash=1_000_000, initial_capital=1_000_000)
        self.deployment = PaperNativeDeployment(portfolio_id=None, strategy_id=None, strategy_version_id=None, status="active")
        await db.flush()
        return self

    async def run(self, state: dict, now: datetime) -> BacktestNativeContext:
        ctx = BacktestNativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=state, now=now.astimezone(timezone.utc))
        await rs.evaluate(ctx)
        return ctx


UP = {"UPA": 0.004, "UPB": 0.003, "FLAT": 0.0, "DOWN": -0.003}


async def test_it_buys_the_top_stocks_as_soon_as_it_starts_at_the_open(db_session, small):
    market = await Market(db_session).build(date(2025, 6, 30), UP)
    ctx = await market.run({}, _ist(date(2025, 6, 24), time(9, 15)))  # a Tuesday
    held = ctx.state["holdings"]
    assert set(held) == {"UPA", "UPB"} and ctx.state["started"] is True
    assert ctx._last_action == "entered" and ctx._last_reason.startswith("started: bought UPA (rank 1), UPB (rank 2)")
    # At 09:15 a backtest buys at that day's opening price, not its close.
    day = (await db_session.execute(
        OhlcvCandle.__table__.select().where(OhlcvCandle.instrument_id == market.stocks["UPA"].id, OhlcvCandle.ts == _day_ts(date(2025, 6, 24)))
    )).first()
    assert held["UPA"]["entry_price"] == pytest.approx(day.open)
    assert held["UPA"]["quantity"] == int(500_000 / day.open)
    assert "last_rebalance_period" not in ctx.state  # Friday's readjust still comes


async def test_it_readjusts_on_friday_at_1500_not_before(db_session, small):
    market = await Market(db_session).build(date(2025, 6, 30), UP)
    state = (await market.run({}, _ist(date(2025, 6, 23), time(9, 15)))).state
    before = await market.run(state, _ist(date(2025, 6, 27), time(14, 55)))
    assert before._last_action == "hold" and "next readjust Fri 27 Jun 15:00" in before._last_reason
    assert before._wake_at == _ist(date(2025, 6, 27), time(15, 0, 3)).astimezone(timezone.utc)
    at = await market.run(before.state, _ist(date(2025, 6, 27), time(15, 0)))
    assert at._last_reason.startswith("readjusted Fri 27 Jun 15:00") and at.state["last_rebalance_period"] == "2025-W26"
    later = await market.run(at.state, _ist(date(2025, 6, 27), time(15, 5)))
    assert later._last_action == "hold" and "next week" in later._last_reason


async def test_the_ranking_moves_with_time_and_never_reads_a_later_candle(db_session, small):
    # UPA/UPB lead until 2 Jun; from then FLAT and DOWN race ahead.
    market = await Market(db_session).build(
        date(2025, 9, 30), UP, switch=(date(2025, 6, 2), {"UPA": -0.004, "UPB": -0.003, "FLAT": 0.006, "DOWN": 0.008}),
    )
    early = await market.run({}, _ist(date(2025, 5, 23), time(15, 0)))
    assert set(early.state["holdings"]) == {"UPA", "UPB"}  # the later reversal isn't seen
    late = await market.run(early.state, _ist(date(2025, 8, 29), time(15, 0)))
    assert set(late.state["holdings"]) == {"FLAT", "DOWN"}
    assert {t["exit_reason"] for t in late.trades} == {"dropped_out_of_top_n"}


async def test_this_weeks_move_counts_at_the_friday_readjust(db_session, small):
    # Flat for months, then DOWN jumps 30% on Monday-Thursday of the readjust week.
    flat = {s: 0.0 for s in STOCKS}
    market = await Market(db_session).build(date(2025, 6, 26), flat, switch=(date(2025, 6, 23), {"DOWN": 0.07}))
    ctx = await market.run({"started": True, "last_rebalance_period": "2025-W25"}, _ist(date(2025, 6, 27), time(15, 0)))
    assert "DOWN" in ctx.state["holdings"] and ctx.state["holdings"]["DOWN"]["rank"] == 1


async def test_a_friday_holiday_moves_the_readjust_to_thursday_from_the_calendar_or_the_data(db_session, small):
    # 18 Apr 2025 (Good Friday) isn't in the seeded calendar; NIFTY 50 has no candle that day.
    market = await Market(db_session).build(date(2025, 4, 30), UP, skip={date(2025, 4, 18)})
    state = {"started": True, "holdings": {}}
    wed = await market.run(dict(state), _ist(date(2025, 4, 16), time(15, 0)))
    assert "next readjust Thu 17 Apr 15:00" in wed._last_reason
    thu = await market.run(wed.state, _ist(date(2025, 4, 17), time(15, 0)))
    assert thu._last_reason.startswith("readjusted Thu 17 Apr 15:00") and thu.state["last_rebalance_period"] == "2025-W16"
    # From the calendar: 3 Apr 2026 (Good Friday) is seeded.
    assert await rs._rebalance_day(thu, market.nifty.id, date(2026, 3, 30)) == date(2026, 4, 2)


async def test_equal_weights_trim_and_top_up_outside_a_one_point_band(db_session, small, monkeypatch):
    market = await Market(db_session).build(date(2025, 6, 30), {s: 0.0 for s in STOCKS} | {"UPA": 0.002, "UPB": 0.001})
    ids = {s: str(i.id) for s, i in market.stocks.items()}
    market.portfolio.cash = 0.0
    opened = _ist(date(2025, 5, 2), time(15, 0)).isoformat()
    state = {"started": True, "last_rebalance_period": "2025-W25", "holdings": {
        # Worth ~70% and ~30% of equity before the readjust: UPA is trimmed, UPB topped up, to ~50% each.
        "UPA": {"instrument_id": ids["UPA"], "quantity": 7000.0, "entry_price": 90.0, "opened_at": opened},
        "UPB": {"instrument_id": ids["UPB"], "quantity": 3000.0, "entry_price": 95.0, "opened_at": opened},
    }}
    ctx = await market.run(state, _ist(date(2025, 6, 27), time(15, 0)))
    held = ctx.state["holdings"]
    prices = await rs._replayed_prices(ctx, [market.stocks["UPA"].id, market.stocks["UPB"].id])
    value = {s: held[s]["quantity"] * prices[market.stocks[s].id] for s in held}
    equity = sum(value.values()) + ctx.portfolio.cash
    assert all(abs(v / equity * 100 - 50) <= 1 for v in value.values())
    [trim] = ctx.trades
    assert trim["exit_reason"] == "trimmed_to_equal_weight" and trim["legs"][0]["entry_price"] == 90.0
    assert held["UPB"]["entry_price"] > 95.0  # averaged up by the top-up
    assert "reweighted UPA, UPB" in ctx._last_reason

    # A week later both sit within the band: nothing is traded.
    again = await market.run({**ctx.state, "last_rebalance_period": "2025-W26"}, _ist(date(2025, 6, 27), time(15, 0)) + timedelta(days=7))
    assert again.trades == [] and again._last_action == "hold"


async def test_stop_and_the_end_of_a_backtest_sell_everything(db_session, small):
    market = await Market(db_session).build(date(2025, 6, 30), UP)
    started = await market.run({}, _ist(date(2025, 6, 24), time(9, 15)))
    ctx = await market.run({**started.state, "force_exit": True}, _ist(date(2025, 6, 30), time(15, 30)))
    assert ctx.state["holdings"] == {} and "force_exit" not in ctx.state
    assert sorted(t["exit_reason"] for t in ctx.trades) == ["backtest_end", "backtest_end"]
    assert all(t["pnl"] > 0 for t in ctx.trades)


async def test_backtest_prices_use_only_finished_intraday_candles(db_session, small):
    market = await Market(db_session).build(date(2025, 6, 27), UP)
    upa = market.stocks["UPA"].id
    for minute, close in ((50, 111.0), (55, 112.0), (60, 999.0)):  # 14:50, 14:55 and the 15:00 candle, still forming at 15:00
        db_session.add(OhlcvCandle(instrument_id=upa, timeframe="5m", ts=(_ist(date(2025, 6, 27), time(14, 0)) + timedelta(minutes=minute)).astimezone(timezone.utc),
                                   open=close, high=close, low=close, close=close, volume=1, source="test"))
    await db_session.flush()
    ctx = await market.run({"started": True, "last_rebalance_period": "2025-W26"}, _ist(date(2025, 6, 27), time(15, 0)))
    prices = await rs._replayed_prices(ctx, [upa, market.stocks["UPB"].id])
    assert prices[upa] == 112.0  # the 14:55 candle, finished at 15:00
    day = (await db_session.execute(
        OhlcvCandle.__table__.select().where(OhlcvCandle.instrument_id == market.stocks["UPB"].id, OhlcvCandle.ts == _day_ts(date(2025, 6, 27)))
    )).first()
    assert prices[market.stocks["UPB"].id] == pytest.approx(day.close)  # no intraday saved: the day's close stands in


async def test_live_it_ranks_and_buys_through_the_live_price_lookup(db_session, small):
    from app.services.paper_trading.native_runner import NativeContext

    market = await Market(db_session).build(date(2025, 6, 27), UP)
    ctx = NativeContext(db=db_session, portfolio=market.portfolio, deployment=market.deployment, state={},
                        now=_ist(date(2025, 6, 27), time(16, 0)).astimezone(timezone.utc))  # after the close: the stored closes
    await rs.evaluate(ctx)
    assert set(ctx.state["holdings"]) == {"UPA", "UPB"} and ctx._last_action == "entered"
    # Friday's 15:00 had passed when it started, so this week counts as readjusted.
    assert ctx.state["last_rebalance_period"] == "2025-W26"
