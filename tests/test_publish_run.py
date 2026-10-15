"""One TA publish run, end to end with the DB and relay faked.

The relay is the only sink: a clean run prunes the baseline to the current
above-cutoff set, an unacked publish fails the run without touching the
baseline, and a forced full relay run resets the every-Nth backstop.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nostr_sdk import Keys

from app.db_models import BrainstormRequestStatus
from app.message_queue_tasks import upload_nostr_events as upload
from app.message_queue_tasks.relay_publisher import PublishStats

OBSERVER = "a" * 64


def _message(changed: list[str]) -> dict:
    def card(observee: str, influence: float) -> dict:
        return {
            "observer": OBSERVER,
            "observee": observee,
            "influence": influence,
            "hops": 1,
        }

    return {
        "private_id": 7,
        "result": {
            "scorecards": {"hi": card("hi", 0.9), "lo": card("lo", 0.01)},
            "duration_seconds": 1.0,
            "changedScorePubkeys": changed,
        },
    }


class _Relay:
    """Stands in for `publish_events`: drains the signed stream, OKs all or none."""

    def __init__(self, fail: bool = False):
        self.sent: list = []
        self.fail = fail

    async def publish(self, url, events) -> PublishStats:
        self.sent = [e async for e in events]
        n = len(self.sent)
        return PublishStats(n_events=n, n_acked=0 if self.fail else n)


@pytest.fixture
def run(monkeypatch):
    keys = Keys.generate()
    relay = _Relay()
    repo = SimpleNamespace(
        persist=AsyncMock(),
        reset=AsyncMock(),
        ta_status=AsyncMock(),
    )
    request_row = SimpleNamespace(force_full_relay=False)

    @asynccontextmanager
    async def _fake_db_session():
        yield AsyncMock()

    nsec_row = SimpleNamespace(
        pubkey=OBSERVER,
        nsec=keys.secret_key().to_hex(),
        assistant_kind0_published_at=datetime(2026, 1, 1),
    )
    monkeypatch.setattr(upload, "db_session", _fake_db_session)
    monkeypatch.setattr(upload, "publish_events", relay.publish)
    monkeypatch.setattr(
        upload,
        "get_or_create_brainstorm_observer_nsec_by_pubkey_on_db",
        AsyncMock(return_value=(nsec_row, False)),
    )
    monkeypatch.setattr(
        upload,
        "select_brainstorm_request_by_id_on_db",
        AsyncMock(side_effect=lambda db, rid: request_row),
    )
    monkeypatch.setattr(
        upload, "update_brainstorm_request_ta_status_by_id_on_db", repo.ta_status
    )
    monkeypatch.setattr(
        upload,
        "get_last_published_pubkeys_by_pubkey_on_db",
        AsyncMock(return_value=["hi", "gone"]),
    )
    monkeypatch.setattr(
        upload, "update_last_published_pubkeys_by_pubkey_on_db", repo.persist
    )
    monkeypatch.setattr(
        upload, "update_last_time_published_graperank_on_db", AsyncMock()
    )
    monkeypatch.setattr(
        upload, "update_brainstorm_request_publish_duration_by_id_on_db", AsyncMock()
    )
    monkeypatch.setattr(upload, "reset_runs_since_full_on_db", repo.reset)
    monkeypatch.setattr(
        upload, "relay_publish_gap_pending_on_db", AsyncMock(return_value=False)
    )

    def _go(changed: list[str], force_full_relay: bool = False) -> None:
        request_row.force_full_relay = force_full_relay
        asyncio.run(upload.process_nostr_upload_message(_message(changed)))

    return SimpleNamespace(go=_go, relay=relay, repo=repo)


def _final_status(repo) -> BrainstormRequestStatus:
    return repo.ta_status.await_args.kwargs["status"]


def _persisted(repo) -> list[str]:
    return sorted(repo.persist.await_args.kwargs["published_pubkeys"])


def test_clean_run_succeeds_and_prunes_the_baseline(run):
    run.go(changed=["hi"])

    assert _final_status(run.repo) == BrainstormRequestStatus.SUCCESS
    # One TA for "hi", one kind-5 for "gone".
    assert len(run.relay.sent) == 2
    assert _persisted(run.repo) == ["hi"]
    run.repo.reset.assert_not_awaited()


def test_unacked_publish_fails_the_run_and_keeps_the_baseline(run):
    run.relay.fail = True

    run.go(changed=["hi"])

    assert _final_status(run.repo) == BrainstormRequestStatus.FAILURE
    run.repo.persist.assert_not_awaited()


def test_full_relay_run_resets_the_backstop(run):
    run.go(changed=[], force_full_relay=True)

    assert len(run.relay.sent) == 2  # "hi" re-asserted though unchanged
    run.repo.reset.assert_awaited_once()
