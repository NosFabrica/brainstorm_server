"""Per-tier manual recalculation quota (pure decision).

Counts only successfully-published manual runs in a rolling window; the I/O
(the count query, tier lookup, and the 429) lives in the router/repo.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.repos.brainstorm_nsec import get_scheduling_for_pubkey_on_db
from app.repos.brainstorm_request_repo import (
    count_successful_manual_runs_in_window_on_db,
)


@dataclass
class QuotaDecision:
    allowed: bool
    reset_at: datetime | None = None


def manual_quota_decision(
    count: int,
    oldest_in_window: datetime | None,
    limit: int,
    window_seconds: int,
    now: datetime,
) -> QuotaDecision:
    if count < limit:
        return QuotaDecision(allowed=True)
    reset_at = (oldest_in_window or now) + timedelta(seconds=window_seconds)
    return QuotaDecision(allowed=False, reset_at=reset_at)


def quota_exceeded_message(
    tier_name: str, limit: int, window_seconds: int, reset_at: datetime
) -> str:
    days = window_seconds / 86400
    window = f"{days:g} day" + ("" if days == 1 else "s")
    return (
        f"Manual recalculation quota reached for tier '{tier_name}': "
        f"{limit} per {window}. Resets at {reset_at.isoformat()}."
    )


async def enforce_manual_quota(db: AsyncDBSession, pubkey: str) -> None:
    """Raise HTTP 429 if the user is at their tier's manual quota."""
    policy = await get_scheduling_for_pubkey_on_db(db, pubkey)
    if policy is None:
        return
    limit = policy.manual_quota_limit
    window = policy.manual_quota_window_seconds
    now = datetime.now()
    window_start = now - timedelta(seconds=window)
    count, oldest = await count_successful_manual_runs_in_window_on_db(
        db, pubkey, window_start
    )
    decision = manual_quota_decision(count, oldest, limit, window, now)
    if not decision.allowed:
        assert decision.reset_at is not None  # always set when denied
        raise HTTPException(
            status_code=429,
            detail=quota_exceeded_message(
                policy.name, limit, window, decision.reset_at
            ),
        )
