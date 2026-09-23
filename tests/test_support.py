"""User-facing support surface: the support state (`GET /user/support`).

Router + service + repo run for real; `get_db` yields a mock session that
records every statement it is handed, and the ticket-list `paginate` is patched
at the service's import site so the listing never needs a real database.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from fastapi_pagination import Page
from sqlalchemy.dialects import postgresql

from app.core.database import get_db
from app.repos.support_repo import build_user_support_tickets_stmt


def _policy(support_included: bool) -> SimpleNamespace:
    return SimpleNamespace(id=1, name="Weekly", support_included=support_included)


def _sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


class _Result:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    """Answers the policy lookup: the user's `scheduling_id`, then that row.

    `policies` is consumed one per request, so a test can change the Policy
    between two requests the way an admin ticking the box would.
    """

    def __init__(self, *policies: SimpleNamespace) -> None:
        self._policies = list(policies)
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if "brainstorm_nsec" in _sql(stmt):
            return _Result(1)
        return _Result(self._policies.pop(0))


@pytest.fixture
def session():
    return _FakeSession(_policy(False))


@pytest.fixture
def paginate(monkeypatch):
    fake = AsyncMock(return_value=Page(items=[], total=0, page=1, size=50, pages=0))
    monkeypatch.setattr("app.services.support_service.paginate", fake)
    return fake


@pytest.fixture
def support_client(client, session, paginate):
    from app.api import app

    async def _fake_get_db():
        yield session

    app.dependency_overrides[get_db] = _fake_get_db
    yield client


@pytest.fixture(autouse=True)
def no_whitelist(monkeypatch):
    monkeypatch.setattr(
        "app.services.support_entitlement.get_whitelisted_pubkeys", lambda: set()
    )


def test_support_is_not_included_on_a_policy_without_it(support_client):
    response = support_client.get("/user/support")

    assert response.status_code == 200
    assert response.json()["data"]["support_included"] is False


def test_support_is_included_when_the_policy_includes_it(support_client, session):
    session._policies = [_policy(True)]

    response = support_client.get("/user/support")

    assert response.json()["data"]["support_included"] is True


def test_default_policy_does_not_include_support(support_client, session):
    async def execute(stmt):
        session.statements.append(stmt)
        sql = _sql(stmt)
        if "brainstorm_nsec" in sql:
            return _Result(None)  # unassigned → falls through to the default
        assert "scheduling.is_default IS true" in sql
        return _Result(_policy(False))

    session.execute = execute

    response = support_client.get("/user/support")

    assert response.json()["data"]["support_included"] is False
    assert len(session.statements) == 2


def test_whitelisted_pubkey_is_always_included(support_client, caller, monkeypatch):
    monkeypatch.setattr(
        "app.services.support_entitlement.get_whitelisted_pubkeys",
        lambda: {caller.pubkey},
    )

    response = support_client.get("/user/support")

    assert response.json()["data"]["support_included"] is True


def test_ticking_the_policy_takes_effect_on_the_next_request(support_client, session):
    session._policies = [_policy(False), _policy(True)]

    first = support_client.get("/user/support").json()["data"]
    second = support_client.get("/user/support").json()["data"]

    assert first["support_included"] is False
    assert second["support_included"] is True


def test_tickets_are_a_page_of_the_callers_own(support_client, paginate, caller):
    response = support_client.get("/user/support?page=2&size=10")

    tickets = response.json()["data"]["tickets"]
    assert set(tickets) == {"items", "total", "page", "size", "pages"}
    _, stmt = paginate.call_args.args
    assert caller.pubkey in _sql(stmt)
    params = paginate.call_args.kwargs["params"]
    assert (params.page, params.size) == (2, 10)


def test_tickets_are_listed_even_when_support_is_not_included(support_client, paginate):
    response = support_client.get("/user/support")

    assert response.json()["data"]["support_included"] is False
    paginate.assert_awaited_once()


def test_get_user_support_is_the_support_state_not_a_user_profile(
    support_client, monkeypatch
):
    # `/user/{pubkey}` is a single-segment catch-all under the same prefix; a
    # route registered after it would answer this path with a profile and a 200.
    profile_lookup = AsyncMock(side_effect=AssertionError("hit /user/{pubkey}"))
    monkeypatch.setattr("app.routers.user.router.get_user_graph_data", profile_lookup)

    response = support_client.get("/user/support")

    assert response.status_code == 200
    assert set(response.json()["data"]) == {"support_included", "tickets"}
    profile_lookup.assert_not_called()


def test_support_state_never_reads_billing_tables(support_client, session, paginate):
    support_client.get("/user/support")

    _, list_stmt = paginate.call_args.args
    sql = " ".join(_sql(s) for s in [*session.statements, list_stmt])
    assert "billing_plan" not in sql
    assert "user_subscription" not in sql


def test_support_state_requires_authentication():
    from app.api import app

    response = TestClient(app).get("/user/support")

    assert response.status_code in (401, 403)


def test_ticket_list_is_the_owners_newest_activity_first():
    sql = _sql(build_user_support_tickets_stmt("b" * 64))

    assert f"support_ticket.pubkey = '{'b' * 64}'" in sql
    assert "ORDER BY support_ticket.last_message_at DESC" in sql
