"""Admin CRUD for the scheduling (tier) table.

DB faked (get_db yields a mock session), repo functions patched at the router
namespace, admin gate opened. The negative-auth path leaves the gate on.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.database import get_db
from app.repos.scheduling_repo import build_scheduling_users_stmt
from app.routers.admin.router import verify_admin_access


def _policy(id=1, name="Weekly", interval=604800, priority=0, enabled=True,
            is_default=True, limit=20, window=604800):
    return SimpleNamespace(
        id=id, name=name, schedule_interval_seconds=interval, priority=priority,
        enabled=enabled, is_default=is_default, manual_quota_limit=limit,
        manual_quota_window_seconds=window,
    )


@pytest.fixture
def admin_client(client):
    from app.api import app

    async def _fake_get_db():
        yield AsyncMock()

    app.dependency_overrides[verify_admin_access] = lambda: None
    app.dependency_overrides[get_db] = _fake_get_db
    yield client


def test_list_scheduling_policies(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.list_scheduling_on_db",
        AsyncMock(return_value=[_policy(id=1, name="Weekly"), _policy(id=2, name="Daily")]),
    )

    response = admin_client.get("/admin/scheduling")

    assert response.status_code == 200
    names = [row["name"] for row in response.json()]
    assert names == ["Weekly", "Daily"]


def test_create_scheduling_policy(admin_client, monkeypatch):
    created = _policy(id=3, name="Daily", interval=86400, priority=1, is_default=False)
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.create_scheduling_on_db", create
    )

    response = admin_client.post(
        "/admin/scheduling",
        json={"name": "Daily", "schedule_interval_seconds": 86400, "priority": 1},
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Daily"
    assert create.await_count == 1


def test_create_as_default_unsets_previous_default(admin_client, monkeypatch):
    unset = AsyncMock()
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.unset_default_scheduling_on_db", unset
    )
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.create_scheduling_on_db",
        AsyncMock(return_value=_policy(id=5, name="D", is_default=True)),
    )

    response = admin_client.post(
        "/admin/scheduling",
        json={"name": "D", "schedule_interval_seconds": 86400, "is_default": True},
    )

    assert response.status_code == 201
    assert unset.await_count == 1  # old default cleared before the new one


def _mock_delete_deps(monkeypatch, policy, user_count):
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.get_scheduling_on_db",
        AsyncMock(return_value=policy),
    )
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.count_users_on_scheduling_on_db",
        AsyncMock(return_value=user_count),
    )
    delete = AsyncMock()
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.delete_scheduling_on_db", delete
    )
    return delete


def test_delete_policy_in_use_returns_409(admin_client, monkeypatch):
    delete = _mock_delete_deps(monkeypatch, _policy(id=2, is_default=False), user_count=3)
    response = admin_client.delete("/admin/scheduling/2")
    assert response.status_code == 409
    assert delete.await_count == 0


def test_delete_default_policy_rejected(admin_client, monkeypatch):
    delete = _mock_delete_deps(monkeypatch, _policy(id=1, is_default=True), user_count=0)
    response = admin_client.delete("/admin/scheduling/1")
    assert response.status_code == 409
    assert delete.await_count == 0


def test_delete_unused_policy(admin_client, monkeypatch):
    delete = _mock_delete_deps(monkeypatch, _policy(id=2, is_default=False), user_count=0)
    response = admin_client.delete("/admin/scheduling/2")
    assert response.status_code == 204
    assert delete.await_count == 1


def test_update_policy_partial(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.scheduling_exists_on_db",
        AsyncMock(return_value=True),
    )
    update = AsyncMock(return_value=_policy(id=2, name="Faster", interval=3600))
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.update_scheduling_on_db", update
    )

    response = admin_client.patch(
        "/admin/scheduling/2",
        json={"name": "Faster", "schedule_interval_seconds": 3600},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Faster"
    # only the provided fields are forwarded (partial update)
    assert update.await_args.kwargs == {
        "name": "Faster",
        "schedule_interval_seconds": 3600,
    }


def test_update_missing_policy_404(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.scheduling_exists_on_db",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.update_scheduling_on_db", AsyncMock()
    )
    response = admin_client.patch("/admin/scheduling/99", json={"name": "X"})
    assert response.status_code == 404


def test_scheduling_users_stmt_filters_by_policy():
    # A specific policy: filter on scheduling_id, no NULL branch.
    stmt = build_scheduling_users_stmt(5, include_null=False)
    assert "pubkey" in stmt.selected_columns.keys()
    assert "IS NULL" not in str(stmt)

    # The default policy also includes unassigned (NULL) users.
    default_stmt = build_scheduling_users_stmt(1, include_null=True)
    assert "IS NULL" in str(default_stmt)


def test_bulk_assign_users_to_policy(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.scheduling_exists_on_db",
        AsyncMock(return_value=True),
    )
    bulk = AsyncMock(return_value=2)
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.bulk_set_scheduling_for_pubkeys_on_db",
        bulk,
    )

    response = admin_client.put(
        "/admin/scheduling/3/users", json={"pubkeys": ["a" * 64, "b" * 64]}
    )

    assert response.status_code == 200
    assert response.json()["assigned"] == 2
    _db, passed_pubkeys, passed_id = bulk.await_args.args
    assert passed_pubkeys == ["a" * 64, "b" * 64]
    assert passed_id == 3


def test_bulk_assign_unknown_policy_404(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.scheduling_exists_on_db",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.bulk_set_scheduling_for_pubkeys_on_db",
        AsyncMock(),
    )
    response = admin_client.put("/admin/scheduling/9/users", json={"pubkeys": ["a" * 64]})
    assert response.status_code == 404
