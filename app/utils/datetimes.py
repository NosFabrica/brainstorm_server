"""Clock helpers for the naive timestamp columns."""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Now in UTC, naive — what the naive timestamp columns hold.

    Bare `datetime.now()` is the app host's local clock, so it skews against
    the columns the database fills with `now()` and can order a `closed_at`
    before the `created_at` beside it.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
