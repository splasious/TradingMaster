import uuid
from datetime import datetime, timedelta, timezone

from app.models.market_data import OhlcvCandle
from app.services.market_data.freshness import check_freshness

# A real, known NSE trading Thursday, well inside market hours (09:15-15:30
# IST == 03:45-10:00 UTC).
MARKET_OPEN_NOW = datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc)  # 14:00 IST
# A Saturday -- market closed regardless of time of day.
MARKET_CLOSED_NOW = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)


def _candle(ts: datetime, timeframe: str = "15m") -> OhlcvCandle:
    return OhlcvCandle(
        id=uuid.uuid4(), instrument_id=uuid.uuid4(), timeframe=timeframe, ts=ts,
        open=1, high=1, low=1, close=1, volume=1, source="zerodha_kite",
    )


def test_check_freshness_none_when_candle_is_recent():
    candles = [_candle(MARKET_OPEN_NOW - timedelta(minutes=5))]
    assert check_freshness(candles, "15m", MARKET_OPEN_NOW) is None


def test_check_freshness_flags_stale_candle_during_market_hours():
    candles = [_candle(MARKET_OPEN_NOW - timedelta(hours=2))]
    reason = check_freshness(candles, "15m", MARKET_OPEN_NOW)
    assert reason is not None
    assert "stale" in reason.lower() or "old" in reason.lower()


def test_check_freshness_flags_empty_candles_during_market_hours():
    reason = check_freshness([], "15m", MARKET_OPEN_NOW)
    assert reason is not None


def test_check_freshness_skips_check_when_market_closed():
    # Same "2 hours old" gap that's flagged during market hours, but the
    # market isn't open -- a normal weekend/overnight gap, not a failure.
    candles = [_candle(MARKET_CLOSED_NOW - timedelta(days=2))]
    assert check_freshness(candles, "15m", MARKET_CLOSED_NOW) is None
    assert check_freshness([], "15m", MARKET_CLOSED_NOW) is None


def test_check_freshness_daily_timeframe_tolerates_a_normal_weekend_gap():
    # Friday's close is ~2.5 calendar days old by Monday market open --
    # must not be flagged as stale.
    friday_close = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)  # Friday 15:30 IST
    monday_open = datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc)  # Monday 09:30 IST
    candles = [_candle(friday_close, timeframe="1d")]
    assert check_freshness(candles, "1d", monday_open) is None


def test_check_freshness_unknown_timeframe_is_not_rejected():
    candles = [_candle(MARKET_OPEN_NOW - timedelta(days=10))]
    assert check_freshness(candles, "4h", MARKET_OPEN_NOW) is None
