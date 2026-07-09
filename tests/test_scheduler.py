"""Behavioral tests for the auto-scheduler decision core.

The scheduler's I/O (Postgres candidate query, Neo4j follow-check, Redis lock,
enqueue) is thin; the decisions that matter — who is overdue, in what order,
and how many to admit — live in pure functions tested here.
"""

import asyncio
from datetime import datetime, timedelta

from app.services.scheduler import (
    SchedulerCandidate,
    admission_budget,
    is_overdue,
    rank_overdue_candidates,
    request_in_pipeline,
)
from app.services.scheduler_lock import acquire_or_renew_leader


class _FakeRedis:
    """Minimal async Redis honoring SET NX/XX semantics (TTL ignored)."""

    def __init__(self):
        self.store = {}

    async def set(self, key, val, nx=False, xx=False, px=None):
        exists = key in self.store
        if nx and exists:
            return None
        if xx and not exists:
            return None
        self.store[key] = val
        return True

    async def get(self, key):
        return self.store.get(key)

NOW = datetime(2026, 1, 1, 12, 0, 0)
DAY = 86400


def _cand(pubkey="a", priority=0, interval=DAY, last_published=None,
          last_failed_at=None, enabled=True):
    return SchedulerCandidate(
        pubkey=pubkey,
        priority=priority,
        interval_seconds=interval,
        last_published=last_published,
        last_failed_at=last_failed_at,
        enabled=enabled,
    )


def test_never_published_user_is_overdue():
    assert is_overdue(_cand(last_published=None), NOW) is True


def test_overdue_only_past_the_tier_cadence():
    fresh = _cand(interval=DAY, last_published=NOW - timedelta(hours=23))
    stale = _cand(interval=DAY, last_published=NOW - timedelta(hours=25))
    assert is_overdue(fresh, NOW) is False
    assert is_overdue(stale, NOW) is True


def test_rank_orders_highest_priority_first():
    low = _cand(pubkey="low", priority=0, last_published=None)
    high = _cand(pubkey="high", priority=2, last_published=None)
    ranked = rank_overdue_candidates([low, high], NOW)
    assert [c.pubkey for c in ranked] == ["high", "low"]


def test_rank_most_overdue_first_within_a_tier():
    recent = _cand(pubkey="recent", priority=0, last_published=NOW - timedelta(days=2))
    ancient = _cand(pubkey="ancient", priority=0, last_published=NOW - timedelta(days=9))
    never = _cand(pubkey="never", priority=0, last_published=None)
    ranked = rank_overdue_candidates([recent, ancient, never], NOW)
    assert [c.pubkey for c in ranked] == ["never", "ancient", "recent"]


def test_disabled_policy_candidate_is_excluded():
    on = _cand(pubkey="on", last_published=None)
    off = _cand(pubkey="off", last_published=None, enabled=False)
    ranked = rank_overdue_candidates([on, off], NOW)
    assert [c.pubkey for c in ranked] == ["on"]


def test_recently_failed_user_excluded_until_backoff_elapses():
    just_failed = _cand(pubkey="just", last_published=None,
                        last_failed_at=NOW - timedelta(minutes=10))
    failed_long_ago = _cand(pubkey="old", last_published=None,
                            last_failed_at=NOW - timedelta(minutes=40))
    ranked = rank_overdue_candidates([just_failed, failed_long_ago], NOW,
                                     retry_backoff_seconds=1800)
    assert [c.pubkey for c in ranked] == ["old"]


def test_admission_budget_is_target_minus_inflight():
    assert admission_budget(target=1, inflight=0, interactive_in_flight=False) == 1
    assert admission_budget(target=3, inflight=1, interactive_in_flight=False) == 2
    assert admission_budget(target=1, inflight=1, interactive_in_flight=False) == 0
    assert admission_budget(target=1, inflight=5, interactive_in_flight=False) == 0


def test_admission_yields_entirely_while_interactive_in_flight():
    assert admission_budget(target=5, inflight=0, interactive_in_flight=True) == 0


def test_request_in_pipeline_excludes_terminal_runs():
    # Still working -> in pipeline.
    assert request_in_pipeline("waiting", "waiting") is True
    assert request_in_pipeline("ongoing", "waiting") is True
    assert request_in_pipeline("success", "waiting") is True   # publish pending
    assert request_in_pipeline("success", "ongoing") is True   # publishing
    # Terminal -> not in pipeline.
    assert request_in_pipeline("success", "success") is False
    assert request_in_pipeline("failure", "waiting") is False  # dead calc must not block
    assert request_in_pipeline("success", "failure") is False  # publish failed = done


def test_only_one_instance_holds_the_leader_lock():
    redis = _FakeRedis()

    async def _run():
        a = await acquire_or_renew_leader(redis, "instance-A", ttl_ms=120000)
        b = await acquire_or_renew_leader(redis, "instance-B", ttl_ms=120000)
        renew = await acquire_or_renew_leader(redis, "instance-A", ttl_ms=120000)
        return a, b, renew

    a, b, renew = asyncio.run(_run())
    assert a is True       # first acquires
    assert b is False      # second is locked out
    assert renew is True   # holder renews
