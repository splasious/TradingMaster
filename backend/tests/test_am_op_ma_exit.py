"""AM OP TRD 15 MIN's MA exit (native_strategies/nifty_pcr_multi_regime.py,
15 Oct): the bull put spread closes on a 15-minute NIFTY 50 close below the
8-candle MA of (O+H+L+C)/4, the bear call spread on one above it, and that
spread opens again only after a later close back on its side; the straddle
has no MA exit."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_pcr_multi_regime as amop
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext

SPOT = 23450.0  # ATM 23500 (a tie rounds up)
EXPIRY = date(2026, 10, 6)


def at(day: int, hh: int, mm: int, ss: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hh, mm, ss, tzinfo=IST)


class Market:
    STRIKE_STEP = 100  # the option chain listed

    def __init__(self, db, monkeypatch, evaluate=None):
        self.db, self.monkeypatch, self.pcr = db, monkeypatch, None
        self.evaluate = evaluate or amop.evaluate
        self.premiums: dict[tuple[float, str], float] = {}  # (strike, CE/PE) -> premium; 100 otherwise

    async def build(self) -> "Market":
        db = self.db
        user = User(email=f"amop_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="AM OP")
        db.add(user)
        await db.flush()
        strategy = Strategy(name="AM OP TRD 15 MIN", owner_id=user.id, code_type="native")
        db.add(strategy)
        await db.flush()
        version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                                  python_code="#", position_sizing={}, risk_rules={})
        self.portfolio = PaperPortfolio(user_id=user.id, name="AM OP", cash=1_000_000.0, initial_capital=1_000_000.0)
        db.add_all([version, self.portfolio])
        await db.flush()
        self.deployment = PaperNativeDeployment(portfolio_id=self.portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                                status=DeploymentStatus.ACTIVE.value, state=None)
        self.nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index", data_source="zerodha_kite",
                                external_ref="NIFTY 50")
        db.add_all([self.deployment, self.nifty])
        await db.flush()
        self.options = [
            Instrument(exchange="NFO", symbol=f"NIFTY26O06{strike}{kind}", name="opt", instrument_type="option", data_source="zerodha_kite",
                       external_ref=f"NIFTY26O06{strike}{kind}", expiry=EXPIRY, strike=float(strike), option_type=kind, lot_size=65,
                       underlying_instrument_id=self.nifty.id)
            for strike in range(22800, 24201, self.STRIKE_STEP) for kind in ("CE", "PE")
        ]
        db.add_all(self.options)
        await db.commit()

        async def get_pcr(ctx, *args, **kwargs):
            return self.pcr

        self.monkeypatch.setattr(NativeContext, "get_pcr", get_pcr)
        return self

    def prices(self, now: datetime, spot: float = SPOT) -> None:
        quotes = [(o, self.premiums.get((o.strike, o.option_type), 100.0)) for o in self.options]
        for instrument, price in [(self.nifty, spot)] + quotes:
            tick_engine.set_real_price(instrument.id, price, source="test")
            tick_engine._real_price_at[instrument.id] = now

    async def candles(self, first: datetime, last: datetime, level: float) -> None:
        ts = first
        while ts <= last:
            self.db.add(OhlcvCandle(instrument_id=self.nifty.id, timeframe="15m", ts=ts.astimezone(timezone.utc),
                                    open=level, high=level, low=level, close=level, volume=None, source="test"))
            ts += timedelta(minutes=15)
        await self.db.commit()

    async def history(self, level: float, until: datetime) -> None:
        """The MA at `level`: 30 Sep 13:00-15:15, then 1 Oct's candles opening before `until`."""
        await self.candles(at(30, 13, 0, month=9), at(30, 15, 15, month=9), level)
        if until > at(1, 9, 15):
            await self.candles(at(1, 9, 15), until - timedelta(minutes=15), level)

    async def check(self, now: datetime, pcr: float | None, spot: float = SPOT) -> NativeContext:
        self.pcr = pcr
        self.prices(now, spot)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await self.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    async def trades(self) -> list[PaperNativeTrade]:
        return (await self.db.execute(select(PaperNativeTrade).order_by(PaperNativeTrade.closed_at))).scalars().all()


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await Market(db_session, monkeypatch).build()
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def test_the_ma_and_which_closes_count():
    bars = [{"open": 100, "high": 120, "low": 90, "close": 110}] * 7 + [{"open": 200, "high": 200, "low": 200, "close": 200}]
    assert amop.moving_average(bars) == pytest.approx((105 * 7 + 200) / 8) and amop.moving_average(bars[:7]) is None
    candle = {"ts": at(1, 10, 0).astimezone(timezone.utc), "close": 99.9}  # ends 10:15
    assert amop.ma_exit_due("bullish", candle, 100, at(1, 10, 5)) and not amop.ma_exit_due("bearish", candle, 100, at(1, 10, 5))
    assert not amop.ma_exit_due("sideways", candle, 100, at(1, 10, 5))  # the straddle has none
    assert not amop.ma_exit_due("bullish", candle, 100, at(1, 10, 15))  # closed by the time it opened: doesn't count


async def test_the_bull_put_spread_closes_on_a_15_min_close_below_the_ma_and_reopens_after_one_above(market):
    await market.history(23400.0, at(1, 10, 0))  # MA 23400
    opened = await market.check(at(1, 10, 0, 5), 1.30)
    assert opened.state["position"]["regime"] == "bullish"

    dip = await market.check(at(1, 10, 5), 1.30, spot=23390.0)  # under the MA mid-candle: held
    assert dip.state["position"] is not None and "MA exit on a 15-min close below it" in dip._last_reason

    await market.candles(at(1, 10, 0), at(1, 10, 0), 23380.0)  # 10:00-10:15 closes under the MA (23397.5)
    out = await market.check(at(1, 10, 15, 40), 1.30, spot=23390.0)
    [trade] = await market.trades()
    assert out.state["position"] is None and trade.exit_reason == "ma_exit"
    assert "bullish spread closed on the MA: 10:15 close 23,380.0 vs MA 23,397.5" in out._last_reason

    wait = await market.check(at(1, 10, 20), 1.30)  # NIFTY back over it, but no 15-min close since
    assert wait.state.get("position") is None and "bullish closed on the MA at 10:15 -- waits for a 15-min close above it" in wait._last_reason

    await market.candles(at(1, 10, 15), at(1, 10, 15), 23480.0)  # 10:15-10:30 closes over the MA (23407.5)
    back = await market.check(at(1, 10, 30, 30), 1.30)
    assert back.state["position"]["regime"] == "bullish" and "_ma_stop" not in back.state


async def test_the_bear_call_spread_closes_on_a_15_min_close_above_the_ma(market):
    await market.history(23500.0, at(1, 10, 0))  # MA 23500
    assert (await market.check(at(1, 10, 0, 5), 0.70)).state["position"]["regime"] == "bearish"
    assert (await market.check(at(1, 10, 5), 0.70, spot=23510.0)).state["position"] is not None  # over it mid-candle
    await market.candles(at(1, 10, 0), at(1, 10, 0), 23520.0)  # closes over the MA (23502.5)
    out = await market.check(at(1, 10, 15, 40), 0.70, spot=23510.0)
    assert out.state["position"] is None and [t.exit_reason for t in await market.trades()] == ["ma_exit"]


async def test_the_straddle_has_no_ma_exit(market):
    await market.history(23400.0, at(1, 10, 0))
    assert (await market.check(at(1, 10, 0, 5), 1.00)).state["position"]["regime"] == "sideways"
    await market.candles(at(1, 10, 0), at(1, 10, 0), 23300.0)  # a close well under the MA
    held = await market.check(at(1, 10, 15, 40), 1.00, spot=23420.0)
    assert held.state["position"] is not None and await market.trades() == []


async def test_a_close_from_before_the_spread_opened_never_closes_it(market):
    await market.history(23400.0, at(1, 9, 45))
    await market.candles(at(1, 9, 45), at(1, 9, 45), 23380.0)  # 09:45-10:00 closed under the MA
    await market.check(at(1, 10, 5), 1.30)
    held = await market.check(at(1, 10, 6), 1.30)
    assert held.state["position"]["regime"] == "bullish" and "last 10:00 close 23,380.0 vs MA 23,397.5" in held._last_reason
