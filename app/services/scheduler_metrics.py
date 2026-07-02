"""Self-measured scheduler capacity metrics (pure).

No hardcoded capacity constant — throughput, demand, and slip are derived each
cycle from ledger numbers. The I/O (the queries + Redis lane depths) and the
admin surface live elsewhere.
"""

from datetime import datetime

_DAY = 86400


def throughput_per_day(count: int, window_seconds: int) -> float:
    """Published successes/day implied by `count` successes over the window."""
    if window_seconds <= 0:
        return 0.0
    return count * _DAY / window_seconds


def demand_per_day(tiers: list[tuple[int, int]]) -> float:
    """Total runs/day demanded: sum of user_count / cadence over each tier."""
    return sum(
        count * _DAY / cadence_seconds
        for count, cadence_seconds in tiers
        if cadence_seconds > 0
    )


def tier_slip(
    oldest_last_published: datetime | None, cadence_seconds: int, now: datetime
) -> float:
    """Seconds the most-overdue user in a tier is past its cadence (0 if fresh)."""
    if oldest_last_published is None:
        return 0.0
    overdue = (now - oldest_last_published).total_seconds() - cadence_seconds
    return max(0.0, overdue)
