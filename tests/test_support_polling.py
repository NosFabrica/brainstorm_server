"""Conditional GET on the two list endpoints.

Polling is the only way anything reaches a user or the team — nothing is
pushed and nothing is emailed — so the UI runs a slow background poller and
this is what keeps that nearly free. It also has to be *right*: a validator
that misses a state change turns the poller into a silent stall nobody would
think to diagnose.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi_pagination import Page
from sqlalchemy.dialects import postgresql

from app.core.database import get_db
from app.routers.admin.router import verify_admin_access
from app.utils.etags import etag_digest

OWNER = "f" * 64
NOW = datetime(2026, 9, 23, 12, 0, 0)


def _sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


class _DigestResult:
    def __init__(self, row):
        self._row = row

    def one(self):
        return self._row


class _Result:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class _PollSession:
    """Answers the policy lookup and the digest aggregate."""

    def __init__(self, *, support_included=True, latest=NOW, count=3) -> None:
        self.policy = SimpleNamespace(
            id=1, name="Priority", support_included=support_included
        )
        self.latest = latest
        self.count = count
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        sql = _sql(stmt)
        if "max(" in sql:
            return _DigestResult((self.latest, self.count))
        if "brainstorm_nsec" in sql:
            return _Result(1)
        return _Result(self.policy)


@pytest.fixture
def session():
    return _PollSession()


@pytest.fixture
def paginate(monkeypatch):
    fake = AsyncMock(return_value=Page(items=[], total=0, page=1, size=50, pages=0))
    monkeypatch.setattr("app.services.support_service.paginate", fake)
    return fake


@pytest.fixture(autouse=True)
def no_whitelist(monkeypatch):
    monkeypatch.setattr(
        "app.services.support_entitlement.get_whitelisted_pubkeys", lambda: set()
    )


def _client(client, session):
    from app.api import app

    async def _fake_get_db():
        yield session

    app.dependency_overrides[get_db] = _fake_get_db
    return client


def _admin_client(client, session):
    from app.api import app

    app.dependency_overrides[verify_admin_access] = lambda: None
    return _client(client, session)


# --- The user's own list ----------------------------------------------------


def test_the_list_carries_a_validator_that_stays_private(client, session, paginate):
    response = _client(client, session).get("/user/support")

    assert response.headers["etag"]
    # Not a shared cache's business: one caller's tickets.
    assert response.headers["cache-control"] == "private, no-cache"


def test_an_unchanged_poll_costs_no_body(client, session, paginate):
    poller = _client(client, session)
    etag = poller.get("/user/support").headers["etag"]

    again = poller.get("/user/support", headers={"If-None-Match": etag})

    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag


def test_an_unchanged_poll_never_reads_the_rows(client, session, paginate):
    poller = _client(client, session)
    etag = poller.get("/user/support").headers["etag"]
    paginate.reset_mock()

    poller.get("/user/support", headers={"If-None-Match": etag})

    # Cheaper than serving the list, or it is not worth doing.
    paginate.assert_not_awaited()


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param({"latest": datetime(2026, 9, 24, 9, 0, 0)}, id="a-ticket-moved"),
        pytest.param({"count": 4}, id="a-ticket-appeared"),
        pytest.param({"support_included": False}, id="the-policy-was-unticked"),
    ],
)
def test_what_the_client_renders_moves_the_validator(client, paginate, changed):
    before = _client(client, _PollSession()).get("/user/support").headers["etag"]

    after = (
        _client(client, _PollSession(**changed)).get("/user/support").headers["etag"]
    )

    assert before != after


def test_the_validator_is_keyed_on_updated_at_not_message_activity(
    client, session, paginate
):
    # Resolving, recategorizing and a message-less reopen all change what is
    # displayed while touching no message. `updated_at` moves for all of them.
    _client(client, session).get("/user/support")

    digest = next(s for s in session.statements if "max(" in _sql(s))
    assert "max(support_ticket.updated_at)" in _sql(digest)
    assert "last_message_at" not in _sql(digest)


@pytest.mark.parametrize(
    "query", [pytest.param("page=2", id="page"), pytest.param("size=10", id="size")]
)
def test_paging_moves_the_validator(client, paginate, query):
    # Or page two comes back unchanged against page one's tag.
    first = _client(client, _PollSession()).get("/user/support").headers["etag"]

    other = (
        _client(client, _PollSession()).get(f"/user/support?{query}").headers["etag"]
    )

    assert first != other


def test_somebody_elses_validator_is_not_mine(client, paginate, caller):
    mine = _client(client, _PollSession()).get("/user/support").headers["etag"]

    assert mine != etag_digest("someone-else", NOW.isoformat(), 3, True, 1, 50)


# --- The admin queue --------------------------------------------------------


def test_the_queue_carries_a_validator(client, session, paginate):
    response = _admin_client(client, session).get("/admin/support/tickets")

    assert response.headers["etag"]
    assert response.headers["cache-control"] == "private, no-cache"


def test_an_unchanged_queue_poll_costs_no_body(client, session, paginate):
    poller = _admin_client(client, session)
    etag = poller.get("/admin/support/tickets").headers["etag"]

    again = poller.get("/admin/support/tickets", headers={"If-None-Match": etag})

    assert again.status_code == 304
    assert again.content == b""


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("status=open", id="status"),
        pytest.param("category=billing", id="category"),
        pytest.param(f"pubkey={OWNER}", id="requester"),
        pytest.param("page=2", id="page"),
    ],
)
def test_every_filter_moves_the_queues_validator(client, paginate, query):
    unfiltered = (
        _admin_client(client, _PollSession())
        .get("/admin/support/tickets")
        .headers["etag"]
    )

    filtered = (
        _admin_client(client, _PollSession())
        .get(f"/admin/support/tickets?{query}")
        .headers["etag"]
    )

    assert unfiltered != filtered


def test_the_queues_digest_respects_the_same_filters(client, session, paginate):
    _admin_client(client, session).get("/admin/support/tickets?status=open")

    digest = next(s for s in session.statements if "max(" in _sql(s))
    assert "support_ticket.status = 'open'" in _sql(digest)
