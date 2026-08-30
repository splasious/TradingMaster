from datetime import date

from app.services.backfill_platform.completeness import compute_completeness


def test_all_filled_produces_one_segment():
    bar_dates = {date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)}
    segments = compute_completeness(bar_dates, date(2024, 1, 1), date(2024, 1, 3), "delta")
    assert len(segments) == 1
    assert segments[0].status == "filled"


def test_gap_in_middle_produces_three_segments():
    bar_dates = {date(2024, 1, 1), date(2024, 1, 3)}
    segments = compute_completeness(bar_dates, date(2024, 1, 1), date(2024, 1, 3), "delta")
    assert [s.status for s in segments] == ["filled", "gap", "filled"]


def test_yahoo_skips_weekends():
    # 2024-01-01 Mon .. 2024-01-07 Sun, no bars at all
    segments = compute_completeness(set(), date(2024, 1, 1), date(2024, 1, 7), "yahoo")
    total_days = sum((s.end - s.start).days + 1 for s in segments)
    assert total_days == 5  # weekends excluded entirely, not counted as gap


def test_delta_does_not_skip_weekends():
    segments = compute_completeness(set(), date(2024, 1, 1), date(2024, 1, 7), "delta")
    total_days = sum((s.end - s.start).days + 1 for s in segments)
    assert total_days == 7  # crypto trades every day
