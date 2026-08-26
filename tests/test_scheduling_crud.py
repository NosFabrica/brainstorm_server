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


# ---------------------------------------------------------------------------
# Values the API must refuse
#
# Swagger's "Try it out" prefills every field from the schema example, so an
# unedited body arrives fully *set* — `exclude_unset` cannot tell it from a
# deliberate one. On staging that renamed a policy to "string" and set its
# cadence to 0. The schema is the only place this can be stopped.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("interval", [0, -1])
def test_a_policy_cannot_be_created_with_a_non_positive_cadence(
    admin_client, monkeypatch, interval
):
    """is_overdue is `age >= interval_seconds`, so 0 makes everyone on the
    policy permanently overdue and the scheduler recalculates them forever."""
    create = AsyncMock()
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.create_scheduling_on_db", create
    )

    response = admin_client.post(
        "/admin/scheduling",
        json={"name": "Broken", "schedule_interval_seconds": interval},
    )

    assert response.status_code == 422
    create.assert_not_awaited()


@pytest.mark.parametrize("interval", [0, -1])
def test_a_policy_cannot_be_patched_to_a_non_positive_cadence(
    admin_client, monkeypatch, interval
):
    update = AsyncMock()
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.scheduling_exists_on_db",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.update_scheduling_on_db", update
    )

    response = admin_client.patch(
        "/admin/scheduling/4", json={"schedule_interval_seconds": interval}
    )

    assert response.status_code == 422
    update.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"manual_quota_limit": 0},
        {"manual_quota_window_seconds": 0},
    ],
)
def test_a_policy_cannot_be_patched_to_a_quota_nobody_can_use(
    admin_client, monkeypatch, body
):
    """A zero limit bars every manual recalculation on the policy; a zero window
    is a rolling period of no length."""
    update = AsyncMock()
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.scheduling_exists_on_db",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.update_scheduling_on_db", update
    )

    response = admin_client.patch("/admin/scheduling/4", json=body)

    assert response.status_code == 422
    update.assert_not_awaited()


def test_the_last_default_policy_cannot_be_un_defaulted(admin_client, monkeypatch):
    """`is_default: false` is written straight through, and the promote-another
    branch only fires on a truthy value — so this would leave no default at all.
    Every unassigned user then has no policy, and the free plan vanishes from
    /billing/plans, with nothing to signal it."""
    update = AsyncMock()
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.scheduling_exists_on_db",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.get_scheduling_on_db",
        AsyncMock(return_value=_policy(id=1, is_default=True)),
    )
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.update_scheduling_on_db", update
    )

    response = admin_client.patch("/admin/scheduling/1", json={"is_default": False})

    assert response.status_code == 409
    update.assert_not_awaited()


def test_un_defaulting_a_policy_that_is_not_the_default_is_a_no_op(
    admin_client, monkeypatch
):
    """Only the last default is protected — saying so about a row that never
    held it would block a legitimate edit."""
    updated = _policy(id=4, name="Paid", interval=86400, is_default=False)
    update = AsyncMock(return_value=updated)
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.scheduling_exists_on_db",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.get_scheduling_on_db",
        AsyncMock(return_value=_policy(id=4, is_default=False)),
    )
    monkeypatch.setattr(
        "app.routers.admin.scheduling.router.update_scheduling_on_db", update
    )

    response = admin_client.patch("/admin/scheduling/4", json={"is_default": False})

    assert response.status_code == 200
    update.assert_awaited_once()
