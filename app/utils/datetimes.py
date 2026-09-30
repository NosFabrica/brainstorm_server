"""Clock helpers for the naive timestamp columns."""

from datetime import datetime, timezone
from typing import overload


def utc_now() -> datetime:
    """Now in UTC, naive — what the naive timestamp columns hold.

    Bare `datetime.now()` is the app host's local clock, so it skews against
    the columns the database fills with `now()` and can order a `closed_at`
    before the `created_at` beside it.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


@overload
def as_naive_utc(dt: datetime) -> datetime:
    ...


@overload
def as_naive_utc(dt: None) -> None:
    ...


@overload
def as_naive_utc(dt: datetime | None) -> datetime | None:
    ...


def as_naive_utc(dt: datetime | None) -> datetime | None:
    """Aware -> naive UTC; naive passes through (already UTC by convention)."""
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
