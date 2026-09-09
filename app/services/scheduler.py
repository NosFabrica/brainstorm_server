"""Auto-scheduler decision core (pure functions).

Given candidate users (tier priority + cadence + freshness clock), decide who is
overdue, in what order, and how many to admit. The loop and all I/O live in
app/cronjobs/scheduler.py; this module has no side effects.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class SchedulerCandidate:
    pubkey: str
    priority: int
    interval_seconds: int
    last_published: datetime | None
    last_failed_at: datetime | None = None
    enabled: bool = True


# Hardcoded: a user whose last run failed is skipped for this long before retry.
RETRY_BACKOFF_SECONDS = 3600  # 1 hour


_NON_TERMINAL_STATUSES = ("waiting", "ongoing")


def request_in_pipeline(status: str, ta_status: str) -> bool:
    """True while a run is still progressing: calc pending/running, or calc done
    and publishing pending/running. A failed calc (status=failure) is terminal
    even though its ta_status may sit at the default 'waiting'."""
    if status in _NON_TERMINAL_STATUSES:
        return True
    return status == "success" and ta_status in _NON_TERMINAL_STATUSES


def choose_admission_lane(
    active_priorities: list[int], admitted_counts: dict[int, int]
) -> int:
    """Which priority lane fills the next admission slot. Weight w(p)=p+1; pick
    the active lane furthest below its weight-proportional share (tie-break to
    the highest lane), so the admitted mix approximates the weights over time."""
    total_weight = sum(p + 1 for p in active_priorities)
    total_admitted = sum(admitted_counts.get(p, 0) for p in active_priorities)

    def deficit(p: int) -> float:
        target = (p + 1) / total_weight
        current = admitted_counts.get(p, 0) / total_admitted if total_admitted else 0.0
        return target - current

    return max(active_priorities, key=lambda p: (deficit(p), p))


def admission_budget(target: int, inflight: int, interactive_in_flight: bool) -> int:
    """How many scheduled runs to admit this cycle. Zero while any Manual/Admin
    run is in the pipeline (yield to interactive); else target minus in-flight."""
    if interactive_in_flight:
        return 0
    return max(0, target - inflight)


def is_overdue(candidate: SchedulerCandidate, now: datetime) -> bool:
    """Overdue = never published, or last publish older than the tier cadence."""
    if candidate.last_published is None:
        return True
    age = (now - candidate.last_published).total_seconds()
    return age >= candidate.interval_seconds


def _overdue_seconds(candidate: SchedulerCandidate, now: datetime) -> float:
    if candidate.last_published is None:
        return float("inf")
    return (now - candidate.last_published).total_seconds()


def _in_retry_backoff(
    candidate: SchedulerCandidate, now: datetime, backoff: int
) -> bool:
    if candidate.last_failed_at is None:
        return False
    return (now - candidate.last_failed_at).total_seconds() < backoff


def rank_overdue_candidates(
    candidates: list[SchedulerCandidate],
    now: datetime,
    retry_backoff_seconds: int = RETRY_BACKOFF_SECONDS,
) -> list[SchedulerCandidate]:
    """Overdue candidates, highest priority first, most-overdue first within a tier.
    Users whose last run failed within the retry backoff are skipped."""
    eligible = [
        c
        for c in candidates
        if c.enabled
        and is_overdue(c, now)
        and not _in_retry_backoff(c, now, retry_backoff_seconds)
    ]
    return sorted(
        eligible, key=lambda c: (c.priority, _overdue_seconds(c, now)), reverse=True
    )
