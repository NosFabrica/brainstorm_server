"""Fast, DB-faked tests for per-user scheduling policy + admin set/view.

The router + repo run for real; only the DB driver is faked (``get_db`` yields a
mock session whose ``execute`` returns canned results) and admin auth is
satisfied by overriding ``verify_admin_access``. The negative auth test
deliberately leaves that override off so the real gate runs.
"""

from unittest.mock import AsyncMock

import pytest

from app.core.database import get_db
from app.db_models import Scheduling
from app.repos.brainstorm_request_repo import build_recent_active_pubkeys_stmt
from app.routers.admin.router import verify_admin_access
from app.routers.admin.users.router import _row_to_user_item

PUBKEY = "a" * 64


def _sched(id_: int, name: str) -> Scheduling:
    return Scheduling(
        id=id_, name=name, schedule_interval_seconds=604800, priority=0
    )


class _FakeResult:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


@pytest.fixture
def fake_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def admin_client(client, fake_session):
    """``client`` (auth'd caller) + admin gate opened + DB faked."""

    async def _fake_get_db():
        yield fake_session

    from app.api import app

    app.dependency_overrides[verify_admin_access] = lambda: None
    app.dependency_overrides[get_db] = _fake_get_db
    yield client
    # `client` fixture clears overrides on teardown.


def test_get_user_detail_defaults_to_default_policy_when_unassigned(
    admin_client, fake_session
):
    # First query (user's scheduling_id) -> NULL; then the default policy row.
    fake_session.execute.side_effect = [
        _FakeResult(None),
        _FakeResult(_sched(1, "Weekly")),
    ]

    response = admin_client.get(f"/admin/users/{PUBKEY}")

    assert response.status_code == 200
    body = response.json()
    assert body["scheduling_id"] == 1
    assert body["scheduling_name"] == "Weekly"


def test_get_user_detail_returns_explicit_assignment(admin_client, fake_session):
    # User has scheduling_id 2; the row is fetched and returned.
    fake_session.execute.side_effect = [
        _FakeResult(2),
        _FakeResult(_sched(2, "Daily")),
    ]

    response = admin_client.get(f"/admin/users/{PUBKEY}")

    assert response.status_code == 200
    body = response.json()
    assert body["scheduling_id"] == 2
    assert body["scheduling_name"] == "Daily"


def test_admin_assign_scheduling_persists_and_echoes(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.users.router.scheduling_exists_on_db",
        AsyncMock(return_value=True),
    )
    setter = AsyncMock()
    monkeypatch.setattr(
        "app.routers.admin.users.router.set_scheduling_for_pubkey_on_db", setter
    )
    monkeypatch.setattr(
        "app.routers.admin.users.router.get_scheduling_on_db",
        AsyncMock(return_value=_sched(3, "Hourly")),
    )

    response = admin_client.put(
        f"/admin/users/{PUBKEY}/scheduling", json={"scheduling_id": 3}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["scheduling_id"] == 3
    assert body["scheduling_name"] == "Hourly"
    assert setter.await_count == 1
    _db, passed_pubkey, passed_id = setter.await_args.args
    assert passed_pubkey == PUBKEY
    assert passed_id == 3


def test_admin_assign_unknown_scheduling_is_rejected_422(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.users.router.scheduling_exists_on_db",
        AsyncMock(return_value=False),
    )
    setter = AsyncMock()
    monkeypatch.setattr(
        "app.routers.admin.users.router.set_scheduling_for_pubkey_on_db", setter
    )

    response = admin_client.put(
        f"/admin/users/{PUBKEY}/scheduling", json={"scheduling_id": 999}
    )

    assert response.status_code == 422
    assert setter.await_count == 0  # rejected before any write


def test_non_admin_cannot_assign_scheduling_403(client, fake_session, monkeypatch):
    # No verify_admin_access override: the real admin gate runs and, with admin
    # disabled / no whitelist in the test env, must reject the caller.
    from app.api import app

    async def _fake_get_db():
        yield fake_session

    setter = AsyncMock()
    monkeypatch.setattr(
        "app.routers.admin.users.router.set_scheduling_for_pubkey_on_db", setter
    )
    app.dependency_overrides[get_db] = _fake_get_db

    response = client.put(
        f"/admin/users/{PUBKEY}/scheduling", json={"scheduling_id": 1}
    )

    assert response.status_code == 403
    assert setter.await_count == 0


class _FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


def test_admin_user_list_exposes_scheduling():
    # The list query selects the policy columns...
    stmt = build_recent_active_pubkeys_stmt()
    cols = stmt.selected_columns.keys()
    assert "scheduling_id" in cols
    assert "scheduling_name" in cols

    from datetime import datetime

    now = datetime(2026, 1, 1)
    base = {
        "pubkey": PUBKEY,
        "times_calculated": 3,
        "last_triggered": now,
        "last_updated": now,
        "latest_status": "success",
        "latest_ta_status": "success",
        "latest_algorithm": "graperank",
        "nsec": None,
    }

    # Unassigned user (NULL) -> default policy name filled in by the transformer.
    unassigned = _row_to_user_item(
        _FakeRow({**base, "scheduling_id": None, "scheduling_name": None}),
        default_scheduling_name="Weekly",
    )
    assert unassigned.scheduling_id is None
    assert unassigned.scheduling_name == "Weekly"

    # Explicitly assigned user keeps their own policy name.
    assigned = _row_to_user_item(
        _FakeRow({**base, "scheduling_id": 2, "scheduling_name": "Daily"}),
        default_scheduling_name="Weekly",
    )
    assert assigned.scheduling_id == 2
    assert assigned.scheduling_name == "Daily"
