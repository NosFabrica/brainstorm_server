"""The upload task's first-run kind-0 gate.

The Assistant must never author kind-30382 Trusted Assertions while it has no
kind-0 profile anywhere — that leaves scores on the relay signed by a key
nothing connects back to the user. Only the in-app flow calls
POST /user/assistantProfile, so the TA upload consumer publishes the profile
itself, once, before the observer's first batch. Best-effort: a failed profile
publish leaves the flag unset (natural retry next run) and never blocks the TA
publish.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.message_queue_tasks import upload_nostr_events as upload


OBSERVER = "a" * 64


@pytest.fixture
def fakes(monkeypatch):
    publish = AsyncMock()
    set_flag = AsyncMock()
    db = AsyncMock()

    @asynccontextmanager
    async def _fake_db_session():
        yield db

    monkeypatch.setattr(upload, "publish_assistant_kind0_for_user", publish)
    monkeypatch.setattr(upload, "update_assistant_kind0_published_at_on_db", set_flag)
    monkeypatch.setattr(upload, "db_session", _fake_db_session)
    return publish, set_flag, db


def test_first_run_publishes_the_profile_and_sets_the_flag(fakes):
    publish, set_flag, db = fakes

    asyncio.run(upload.ensure_assistant_kind0_published(OBSERVER, None, {}))

    publish.assert_awaited_once_with(db, user_pubkey=OBSERVER)
    set_flag.assert_awaited_once_with(db, pubkey=OBSERVER)
    db.commit.assert_awaited_once()


def test_an_already_profiled_assistant_is_left_alone(fakes):
    publish, set_flag, _ = fakes

    asyncio.run(
        upload.ensure_assistant_kind0_published(OBSERVER, datetime(2026, 8, 24), {})
    )

    publish.assert_not_awaited()
    set_flag.assert_not_awaited()


def test_a_failed_publish_leaves_the_flag_unset_and_never_raises(fakes):
    publish, set_flag, db = fakes
    publish.side_effect = Exception("all relays down")

    # Must not raise — the TA publish this gate precedes is never blocked.
    asyncio.run(upload.ensure_assistant_kind0_published(OBSERVER, None, {}))

    set_flag.assert_not_awaited()
    db.commit.assert_not_awaited()
