import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from app.services.strategy.native_strategies.nifty_pcr_credit_spread import (
    determine_bias,
    evaluate,
    in_entry_window,
    past_exit_cutoff,
    round_to_nearest_100,
)


def test_round_to_nearest_100_picks_the_closer_strike():
    assert round_to_nearest_100(24930) == 24900
    assert round_to_nearest_100(24960) == 25000


def test_round_to_nearest_100_tie_break():
    assert round_to_nearest_100(24950, tie_break="up") == 25000
    assert round_to_nearest_100(24950, tie_break="down") == 24900


def test_determine_bias():
    assert determine_bias(0.8) == "bearish"
    assert determine_bias(1.2) == "bullish"
    assert determine_bias(1.0) == "neutral"


def test_entry_and_exit_window():
    from datetime import time as dtime

    assert in_entry_window(dtime(9, 45)) is True
    assert in_entry_window(dtime(9, 44)) is False
    assert in_entry_window(dtime(14, 59)) is True
    assert past_exit_cutoff(dtime(15, 0)) is True
    assert past_exit_cutoff(dtime(14, 59)) is False


async def _setup(db_session: AsyncSession, *, cash: float = 1000000.0):
    role = Role(name=f"native_pcr_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"native_pcr_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Native PCR User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    strategy = Strategy(name="Nifty PCR Credit Spread", owner_id=user.id, code_type="native")
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d", instrument_ids=[], parameters={},
        entry_rules=None, exit_rules=None, python_code="# see native_strategies/nifty_pcr_credit_spread.py",
        position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={},
    )
    db_session.add(version)
    await db_session.flush()

    portfolio = PaperPortfolio(user_id=user.id, cash=cash, initial_capital=cash)
    db_session.add(portfolio)
    await db_session.flush()

    deployment = PaperNativeDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
        status=DeploymentStatus.ACTIVE.value, state=None,
    )
    db_session.add(deployment)

    underlying = Instrument(
        exchange="NSE", symbol="NIFTY 50", name="Nifty 50", instrument_type="index",
        data_source="zerodha_kite", external_ref="NIFTY 50",
    )
    db_session.add(underlying)
    await db_session.flush()

    return {"user": user, "portfolio": portfolio, "deployment": deployment, "underlying": underlying}


def _option(underlying_id, strike, option_type, expiry, lot_size=75):
    return Instrument(
        exchange="NFO", symbol=f"NIFTY{expiry.strftime('%y%b').upper()}{int(strike)}{option_type}",
        name=f"NIFTY {strike} {option_type}", instrument_type="option", data_source="zerodha_kite",
        external_ref=f"NIFTY{expiry.strftime('%y%b').upper()}{int(strike)}{option_type}",
        strike=float(strike), option_type=option_type, expiry=expiry, lot_size=lot_size,
        underlying_instrument_id=underlying_id,
    )


async def _seed_oi(db_session, instrument_id, timeframe, oi):
    db_session.add(
        OhlcvCandle(
            instrument_id=instrument_id, timeframe=timeframe, ts=datetime(2026, 9, 17, 4, 0, tzinfo=timezone.utc),
            open=100, high=100, low=100, close=100, volume=0, open_interest=oi, source="test",
        )
    )


async def test_evaluate_enters_a_bear_call_spread_on_bearish_pcr(db_session: AsyncSession):
    ctx_data = await _setup(db_session)
    underlying = ctx_data["underlying"]
    expiry = date(2026, 9, 18)  # not "today" (9/17) -- no roll-to-next-week needed

    short_inst = _option(underlying.id, 25000, "CE", expiry)
    long_inst = _option(underlying.id, 25200, "CE", expiry)
    put_inst = _option(underlying.id, 24800, "PE", expiry)
    db_session.add_all([short_inst, long_inst, put_inst])
    await db_session.flush()
    await _seed_oi(db_session, short_inst.id, "15m", 600)
    await _seed_oi(db_session, long_inst.id, "15m", 400)
    await _seed_oi(db_session, put_inst.id, "15m", 500)  # total call OI 1000, put OI 500 -> PCR 0.5 (bearish)
    await db_session.commit()

    tick_engine.set_real_price(underlying.id, 25000.0, "test")
    tick_engine.set_real_price(short_inst.id, 150.0, "test")
    tick_engine.set_real_price(long_inst.id, 50.0, "test")

    now = datetime(2026, 9, 17, 10, 0, tzinfo=IST)
    ctx = NativeContext(db=db_session, portfolio=ctx_data["portfolio"], deployment=ctx_data["deployment"], state={}, now=now)
    starting_cash = ctx.portfolio.cash

    await evaluate(ctx)

    assert ctx._last_action == "entered"
    position = ctx.state["position"]
    assert position["bias"] == "bearish"
    assert position["short"]["strike"] == 25000
    assert position["long"]["strike"] == 25200
    assert position["short"]["quantity"] == 150.0  # 2 lots * lot_size 75
    assert position["entry_spot"] == 25000.0  # rollover distance is measured from this
    # Net credit = (150 - 50) * 150 = 15000
    assert ctx.portfolio.cash == starting_cash + 15000.0


async def test_evaluate_rolls_the_spread_when_spot_moves_100_points(db_session: AsyncSession):
    ctx_data = await _setup(db_session)
    underlying = ctx_data["underlying"]
    expiry = date(2026, 9, 18)

    old_short = _option(underlying.id, 25000, "CE", expiry)
    old_long = _option(underlying.id, 25200, "CE", expiry)
    new_short = _option(underlying.id, 25100, "CE", expiry)
    new_long = _option(underlying.id, 25300, "CE", expiry)
    put_inst = _option(underlying.id, 24800, "PE", expiry)
    db_session.add_all([old_short, old_long, new_short, new_long, put_inst])
    await db_session.flush()
    for inst, oi in [(old_short, 600), (old_long, 400), (new_short, 100), (new_long, 100), (put_inst, 500)]:
        await _seed_oi(db_session, inst.id, "15m", oi)
    await db_session.commit()

    tick_engine.set_real_price(underlying.id, 25100.0, "test")  # 100-point move from the 25000 entry strike
    tick_engine.set_real_price(old_short.id, 200.0, "test")
    tick_engine.set_real_price(old_long.id, 90.0, "test")
    tick_engine.set_real_price(new_short.id, 180.0, "test")
    tick_engine.set_real_price(new_long.id, 70.0, "test")

    now = datetime(2026, 9, 17, 11, 0, tzinfo=IST)
    state = {
        "position": {
            "bias": "bearish", "pcr_at_entry": 0.5, "entry_spot": 25000.0, "expiry": expiry.isoformat(),
            "short": {"instrument_id": str(old_short.id), "strike": 25000.0, "quantity": 150.0, "entry_price": 150.0},
            "long": {"instrument_id": str(old_long.id), "strike": 25200.0, "quantity": 150.0, "entry_price": 50.0},
            "opened_at": datetime(2026, 9, 17, 10, 0, tzinfo=IST).isoformat(),
        }
    }
    ctx = NativeContext(db=db_session, portfolio=ctx_data["portfolio"], deployment=ctx_data["deployment"], state=state, now=now)
    starting_cash = ctx.portfolio.cash

    await evaluate(ctx)

    # Rolled, not just exited -- a new position must be open at the new ATM.
    new_position = ctx.state["position"]
    assert new_position is not None
    assert new_position["short"]["strike"] == 25100.0
    assert new_position["long"]["strike"] == 25300.0
    assert new_position["entry_spot"] == 25100.0  # the rollover window resets from here

    trades = (await db_session.execute(select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == ctx_data["deployment"].id))).scalars().all()
    assert len(trades) == 1
    assert trades[0].exit_reason == "rollover"

    assert trades[0].pnl == (100.0 - 110.0) * 150.0  # entry credit 100/share vs exit debit 110/share
    # This test seeds `position` directly (skipping a real entry), so
    # starting_cash never received the original entry credit -- the only
    # real cash movement this tick is closing the old legs and opening the
    # new ones: close (buy back short -30000, sell long +13500 = -16500)
    # + open (sell new short +27000, buy new long -10500 = +16500) = 0 net.
    assert ctx.portfolio.cash == starting_cash


async def test_evaluate_exits_on_pcr_flip_without_reopening(db_session: AsyncSession):
    ctx_data = await _setup(db_session)
    underlying = ctx_data["underlying"]
    expiry = date(2026, 9, 18)

    short_inst = _option(underlying.id, 25000, "CE", expiry)
    long_inst = _option(underlying.id, 25200, "CE", expiry)
    put_inst = _option(underlying.id, 24800, "PE", expiry)
    db_session.add_all([short_inst, long_inst, put_inst])
    await db_session.flush()
    # Now bullish: put OI 1500 vs call OI 1000 -> PCR 1.5
    await _seed_oi(db_session, short_inst.id, "15m", 600)
    await _seed_oi(db_session, long_inst.id, "15m", 400)
    await _seed_oi(db_session, put_inst.id, "15m", 1500)
    await db_session.commit()

    tick_engine.set_real_price(underlying.id, 25000.0, "test")
    tick_engine.set_real_price(short_inst.id, 140.0, "test")
    tick_engine.set_real_price(long_inst.id, 45.0, "test")

    now = datetime(2026, 9, 17, 12, 0, tzinfo=IST)
    state = {
        "position": {
            "bias": "bearish", "pcr_at_entry": 0.5, "expiry": expiry.isoformat(),
            "short": {"instrument_id": str(short_inst.id), "strike": 25000.0, "quantity": 150.0, "entry_price": 150.0},
            "long": {"instrument_id": str(long_inst.id), "strike": 25200.0, "quantity": 150.0, "entry_price": 50.0},
            "opened_at": datetime(2026, 9, 17, 10, 0, tzinfo=IST).isoformat(),
        }
    }
    ctx = NativeContext(db=db_session, portfolio=ctx_data["portfolio"], deployment=ctx_data["deployment"], state=state, now=now)

    await evaluate(ctx)

    assert ctx._last_action == "exited"
    assert ctx.state["position"] is None
    trades = (await db_session.execute(select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == ctx_data["deployment"].id))).scalars().all()
    assert len(trades) == 1
    assert trades[0].exit_reason == "pcr_flipped_to_bullish"


async def test_evaluate_exits_at_3pm_cutoff(db_session: AsyncSession):
    ctx_data = await _setup(db_session)
    underlying = ctx_data["underlying"]
    expiry = date(2026, 9, 18)
    short_inst = _option(underlying.id, 25000, "CE", expiry)
    long_inst = _option(underlying.id, 25200, "CE", expiry)
    put_inst = _option(underlying.id, 24800, "PE", expiry)
    db_session.add_all([short_inst, long_inst, put_inst])
    await db_session.flush()
    await _seed_oi(db_session, short_inst.id, "15m", 600)
    await _seed_oi(db_session, long_inst.id, "15m", 400)
    await _seed_oi(db_session, put_inst.id, "15m", 500)
    await db_session.commit()

    tick_engine.set_real_price(underlying.id, 25000.0, "test")
    tick_engine.set_real_price(short_inst.id, 150.0, "test")
    tick_engine.set_real_price(long_inst.id, 50.0, "test")

    now = datetime(2026, 9, 17, 15, 0, tzinfo=IST)  # exactly the cutoff
    state = {
        "position": {
            "bias": "bearish", "pcr_at_entry": 0.5, "expiry": expiry.isoformat(),
            "short": {"instrument_id": str(short_inst.id), "strike": 25000.0, "quantity": 150.0, "entry_price": 150.0},
            "long": {"instrument_id": str(long_inst.id), "strike": 25200.0, "quantity": 150.0, "entry_price": 50.0},
            "opened_at": datetime(2026, 9, 17, 10, 0, tzinfo=IST).isoformat(),
        }
    }
    ctx = NativeContext(db=db_session, portfolio=ctx_data["portfolio"], deployment=ctx_data["deployment"], state=state, now=now)

    await evaluate(ctx)

    assert ctx.state["position"] is None
    trades = (await db_session.execute(select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == ctx_data["deployment"].id))).scalars().all()
    assert trades[0].exit_reason == "time_cutoff_3pm"


async def test_evaluate_rolls_instead_of_exiting_when_short_strike_is_breached(db_session: AsyncSession):
    """A gap move straight through the short strike is not a separate
    "breach" exit anymore -- there's no such thing, per the user's request
    that the only exit triggers are a PCR flip or a 3pm cutoff. Any move of
    100+ points from entry_spot (breach included) is just a big enough move
    to trip the same rollover check: close the tested spread and
    immediately reopen at the new ATM, same bias."""
    ctx_data = await _setup(db_session)
    underlying = ctx_data["underlying"]
    expiry = date(2026, 9, 18)
    short_inst = _option(underlying.id, 25000, "CE", expiry)
    long_inst = _option(underlying.id, 25200, "CE", expiry)  # new ATM (25150 rounds to 25200) -- this same
    # 25200 CE contract becomes the new short leg after the roll, since it's
    # a real option chain lookup by (expiry, strike, type), not a fresh row.
    new_long = _option(underlying.id, 25400, "CE", expiry)
    put_inst = _option(underlying.id, 24800, "PE", expiry)
    db_session.add_all([short_inst, long_inst, new_long, put_inst])
    await db_session.flush()
    # Keep PCR bearish (unchanged) so the flip check doesn't fire first.
    await _seed_oi(db_session, short_inst.id, "15m", 600)
    await _seed_oi(db_session, long_inst.id, "15m", 400)
    await _seed_oi(db_session, put_inst.id, "15m", 500)
    await db_session.commit()

    # Spot gapped from the 25000 entry to 25150 -- 150pts, past the short
    # strike itself and well beyond the 100pt rollover threshold.
    tick_engine.set_real_price(underlying.id, 25150.0, "test")
    tick_engine.set_real_price(short_inst.id, 250.0, "test")
    tick_engine.set_real_price(long_inst.id, 100.0, "test")  # old long's exit price == new short's entry price
    tick_engine.set_real_price(new_long.id, 60.0, "test")

    now = datetime(2026, 9, 17, 13, 0, tzinfo=IST)
    state = {
        "position": {
            "bias": "bearish", "pcr_at_entry": 0.5, "entry_spot": 25000.0, "expiry": expiry.isoformat(),
            "short": {"instrument_id": str(short_inst.id), "strike": 25000.0, "quantity": 150.0, "entry_price": 150.0},
            "long": {"instrument_id": str(long_inst.id), "strike": 25200.0, "quantity": 150.0, "entry_price": 50.0},
            "opened_at": datetime(2026, 9, 17, 10, 0, tzinfo=IST).isoformat(),
        }
    }
    ctx = NativeContext(db=db_session, portfolio=ctx_data["portfolio"], deployment=ctx_data["deployment"], state=state, now=now)

    await evaluate(ctx)

    new_position = ctx.state["position"]
    assert new_position is not None  # rolled into a fresh spread, not left flat
    assert new_position["short"]["strike"] == 25200.0
    assert new_position["long"]["strike"] == 25400.0
    assert new_position["entry_spot"] == 25150.0
    trades = (await db_session.execute(select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == ctx_data["deployment"].id))).scalars().all()
    assert trades[0].exit_reason == "rollover"


async def test_evaluate_holds_when_spot_move_is_under_100_points(db_session: AsyncSession):
    ctx_data = await _setup(db_session)
    underlying = ctx_data["underlying"]
    expiry = date(2026, 9, 18)
    short_inst = _option(underlying.id, 25000, "CE", expiry)
    long_inst = _option(underlying.id, 25200, "CE", expiry)
    put_inst = _option(underlying.id, 24800, "PE", expiry)
    db_session.add_all([short_inst, long_inst, put_inst])
    await db_session.flush()
    await _seed_oi(db_session, short_inst.id, "15m", 600)
    await _seed_oi(db_session, long_inst.id, "15m", 400)
    await _seed_oi(db_session, put_inst.id, "15m", 500)  # PCR unchanged (bearish)
    await db_session.commit()

    tick_engine.set_real_price(underlying.id, 25060.0, "test")  # only 60pts from entry_spot
    tick_engine.set_real_price(short_inst.id, 170.0, "test")
    tick_engine.set_real_price(long_inst.id, 60.0, "test")

    now = datetime(2026, 9, 17, 13, 0, tzinfo=IST)
    state = {
        "position": {
            "bias": "bearish", "pcr_at_entry": 0.5, "entry_spot": 25000.0, "expiry": expiry.isoformat(),
            "short": {"instrument_id": str(short_inst.id), "strike": 25000.0, "quantity": 150.0, "entry_price": 150.0},
            "long": {"instrument_id": str(long_inst.id), "strike": 25200.0, "quantity": 150.0, "entry_price": 50.0},
            "opened_at": datetime(2026, 9, 17, 10, 0, tzinfo=IST).isoformat(),
        }
    }
    ctx = NativeContext(db=db_session, portfolio=ctx_data["portfolio"], deployment=ctx_data["deployment"], state=state, now=now)

    await evaluate(ctx)

    assert ctx._last_action == "hold"
    assert ctx.state["position"] is not None
    assert ctx.state["position"]["short"]["strike"] == 25000.0  # unchanged -- no roll
    trades = (await db_session.execute(select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == ctx_data["deployment"].id))).scalars().all()
    assert trades == []


async def test_evaluate_skips_outside_entry_window(db_session: AsyncSession):
    ctx_data = await _setup(db_session)
    tick_engine.set_real_price(ctx_data["underlying"].id, 25000.0, "test")
    now = datetime(2026, 9, 17, 9, 0, tzinfo=IST)  # before 9:45
    ctx = NativeContext(db=db_session, portfolio=ctx_data["portfolio"], deployment=ctx_data["deployment"], state={}, now=now)

    await evaluate(ctx)

    assert ctx._last_action == "skipped"
    assert ctx.state.get("position") is None
