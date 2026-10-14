"""Admin relay audit: the window walk over a fake relay, and the endpoint."""

import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from nostr_sdk import Keys

from app.core.database import get_db
from app.routers.admin.router import verify_admin_access
from app.services import relay_audit_service as svc

OBSERVER = "a" * 64


class FakeRelay:
    """Events as (created_at, d); honours since/until/limit like strfry."""

    def __init__(self, events):
        self.events = events
        self.downloads: list[int] = []

    def _in(self, since, until):
        return [e for e in self.events if since <= e[0] <= until]

    async def count(self, since, until):
        return len(self._in(since, until))

    async def d_tags(self, since, until, limit, into):
        window = sorted(self._in(since, until), reverse=True)[:limit]
        self.downloads.append(len(window))
        into.update(d for _, d in window)


def _walk(events, until=10_000):
    relay = FakeRelay(events)
    into: set[str] = set()
    asyncio.run(svc._walk(relay, 0, until, len(events), into))
    return into, relay


def test_burst_seconds_are_collected_whole(monkeypatch):
    monkeypatch.setattr(svc, "WINDOW_EVENTS", 3)
    # 5 TAs share second 100 — paging by `until` would stall here
    events = [(100, f"b{i}") for i in range(5)] + [(t, f"s{t}") for t in range(1, 8)]
    got, relay = _walk(events)
    assert got == {d for _, d in events}
    assert max(relay.downloads) <= 5


def test_windows_never_exceed_the_page_size_unless_one_second(monkeypatch):
    monkeypatch.setattr(svc, "WINDOW_EVENTS", 4)
    events = [(t, f"d{t}") for t in range(1, 50)]
    got, relay = _walk(events)
    assert len(got) == 49
    assert max(relay.downloads) <= 4
    assert sum(relay.downloads) == 49


def test_a_second_too_dense_to_download_is_refused(monkeypatch):
    monkeypatch.setattr(svc, "WINDOW_EVENTS", 2)
    monkeypatch.setattr(svc, "MAX_SECOND_EVENTS", 3)
    with pytest.raises(HTTPException) as exc:
        _walk([(7, f"x{i}") for i in range(4)])
    assert exc.value.status_code == 503


def test_needs_resync_only_when_something_is_missing():
    base = dict(observer=OBSERVER, signer="s", published=10, on_relay=12)
    assert svc.RelayAudit(**base, missing=0, orphans=2).needs_resync is False
    assert svc.RelayAudit(**base, missing=1, orphans=3).needs_resync is True


@pytest.fixture
def admin_client(client, monkeypatch):
    from app.api import app

    async def _fake_get_db():
        yield AsyncMock()

    monkeypatch.setattr(
        f"{svc.__name__}.select_brainstorm_nsec_by_pubkey_on_db",
        AsyncMock(
            return_value=SimpleNamespace(nsec=Keys.generate().secret_key().to_hex())
        ),
    )
    monkeypatch.setattr(
        f"{svc.__name__}.get_last_published_pubkeys_by_pubkey_on_db",
        AsyncMock(return_value=["p1", "p2", "p3", "p4"]),
    )
    app.dependency_overrides[verify_admin_access] = lambda: None
    app.dependency_overrides[get_db] = _fake_get_db
    yield client


def test_endpoint_reports_exact_missing_and_orphans(admin_client, monkeypatch):
    monkeypatch.setattr(
        f"{svc.__name__}.read_relay_d_tags",
        AsyncMock(return_value=(4, {"p1", "p2", "p3", "stale"})),
    )

    response = admin_client.get(f"/admin/users/{OBSERVER}/relay-audit")

    assert response.status_code == 200
    data = response.json()["data"]
    assert Counter(
        {k: data[k] for k in ("published", "on_relay", "missing", "orphans")}
    ) == Counter(published=4, on_relay=4, missing=1, orphans=1)
    assert data["needs_resync"] is True


def test_endpoint_502s_when_the_relay_is_unreachable(admin_client, monkeypatch):
    monkeypatch.setattr(
        f"{svc.__name__}.read_relay_d_tags", AsyncMock(side_effect=OSError("down"))
    )

    response = admin_client.get(f"/admin/users/{OBSERVER}/relay-audit")

    assert response.status_code == 502
    assert "TA relay" in response.json()["detail"]
