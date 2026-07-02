"""Behavioral tests for the per-tier manual recalculation quota.

`manual_quota_decision` is the pure seam: given how many successful manual runs
a user has in the rolling window (and the oldest one's time), decide whether a
new trigger is allowed and, if not, when the quota next frees up.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.database import get_db
from app.services.manual_quota import (
    enforce_manual_quota,
    manual_quota_decision,
    quota_exceeded_message,
)

NOW = datetime(2026, 1, 8, 12, 0, 0)
WEEK = 7 * 86400
PK = "a" * 64


def _policy(limit=20, window=WEEK, name="Weekly"):
    return SimpleNamespace(
        name=name, manual_quota_limit=limit, manual_quota_window_seconds=window
    )


def _mock_quota(monkeypatch, policy, count, oldest):
    monkeypatch.setattr(
        "app.services.manual_quota.get_scheduling_for_pubkey_on_db",
        AsyncMock(return_value=policy),
    )
    monkeypatch.setattr(
        "app.services.manual_quota.count_successful_manual_runs_in_window_on_db",
        AsyncMock(return_value=(count, oldest)),
    )


def _fake_db(client):
    from app.api import app

    async def _gen():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _gen


def test_under_limit_is_allowed():
    decision = manual_quota_decision(
        count=5, oldest_in_window=None, limit=20, window_seconds=WEEK, now=NOW
    )
    assert decision.allowed is True


def test_at_limit_is_denied_and_resets_when_oldest_ages_out():
    oldest = NOW - timedelta(days=6)  # 6 days into the 7-day window
    decision = manual_quota_decision(
        count=20, oldest_in_window=oldest, limit=20, window_seconds=WEEK, now=NOW
    )
    assert decision.allowed is False
    assert decision.reset_at == oldest + timedelta(seconds=WEEK)  # 1 day from now


def test_message_states_tier_limit_window_and_reset():
    reset = NOW + timedelta(days=1)
    msg = quota_exceeded_message("Weekly", limit=20, window_seconds=WEEK, reset_at=reset)
    assert "Weekly" in msg          # tier
    assert "20" in msg              # limit
    assert "7" in msg               # window (days)
    assert reset.isoformat() in msg  # reset time


def test_manual_trigger_over_quota_returns_429(client, monkeypatch):
    _mock_quota(monkeypatch, _policy(), count=20, oldest=datetime.now() - timedelta(days=6))
    create = AsyncMock()
    monkeypatch.setattr("app.routers.user.router.create_brainstorm_request", create)
    _fake_db(client)

    response = client.post("/user/graperank")

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert "Weekly" in detail and "20" in detail
    assert create.await_count == 0  # never created a run


def test_under_quota_does_not_block(monkeypatch):
    _mock_quota(monkeypatch, _policy(), count=5, oldest=None)
    # Should not raise (the endpoint then proceeds to create the run).
    asyncio.run(enforce_manual_quota(AsyncMock(), PK))
