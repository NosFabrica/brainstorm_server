"""Behavioral tests for the scheduler's self-measured capacity metrics.

Pure computations over ledger-derived numbers: realized throughput, demand,
per-tier slip, and the optional auto-relax of an overloaded lane's cadence.
"""

from datetime import datetime, timedelta

from app.services.scheduler_metrics import (
    demand_per_day,
    throughput_per_day,
    tier_slip,
)

DAY = 86400
NOW = datetime(2026, 1, 15, 12, 0, 0)


def test_throughput_per_day_scales_window_to_a_day():
    # 35 published successes in a 6h window -> 140/day.
    assert throughput_per_day(count=35, window_seconds=6 * 3600) == 140


def test_demand_per_day_sums_tier_counts_over_cadences():
    # 70 weekly users -> 10/day; 5 daily users -> 5/day; total 15/day.
    assert demand_per_day([(70, 7 * DAY), (5, DAY)]) == 15


def test_tier_slip_is_overdue_beyond_cadence():
    # oldest published 10 days ago, cadence 7 days -> 3 days slip.
    slip = tier_slip(NOW - timedelta(days=10), cadence_seconds=7 * DAY, now=NOW)
    assert slip == 3 * DAY


def test_tier_slip_zero_when_within_cadence_or_no_data():
    assert tier_slip(NOW - timedelta(days=5), 7 * DAY, NOW) == 0
    assert tier_slip(None, 7 * DAY, NOW) == 0
