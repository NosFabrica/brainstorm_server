"""The responder side of support: the queue, and answering it.

Router + service + repo run for real; `get_db` yields a mock session and the
admin gate is opened by overriding `verify_admin_access`. The last test
deliberately leaves that override off, so the real gate runs.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi_pagination import Page
from sqlalchemy.dialects import postgresql

from app.core.database import get_db
from app.db_models import SupportEvent, SupportMessage, SupportTicket
from app.repos.support_repo import build_admin_support_tickets_stmt
from app.routers.admin.router import verify_admin_access

ADMIN = "a" * 64
OWNER = "f" * 64


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


class _ScalarsResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


def _ticket(**overrides) -> SupportTicket:
    now = datetime(2026, 9, 23, 12, 0, 0)
    row = SupportTicket(
        pubkey=OWNER,
        subject="Scores look wrong",
        category="scores",
        status="open",
        notify_email="someone@example.com",
        diagnostics={"App": "v0.1.0-alpha"},
        last_message_at=now,
        last_message_author="user",
        closed_at=None,
    )
    row.id = 7
    row.created_at = now
    row.updated_at = now
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class _AdminSession:
    def __init__(self, ticket: SupportTicket | None) -> None:
        self.ticket = ticket
        self.statements: list = []
        self.added: list = []
        self._next_id = 100

    async def execute(self, stmt):
        self.statements.append(stmt)
        sql = _sql(stmt)
        if "FROM support_message" in sql:
            return _ScalarsResult([])
        if "FROM support_event" in sql:
            return _ScalarsResult([])
        return _Result(self.ticket)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        now = datetime(2026, 9, 23, 12, 30, 0)
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1
            for column in ("created_at", "at"):
                if hasattr(type(obj), column) and getattr(obj, column) is None:
                    setattr(obj, column, now)

    async def refresh(self, obj) -> None:
        pass

    def of(self, model) -> list:
        return [o for o in self.added if isinstance(o, model)]


@pytest.fixture
def session():
    return _AdminSession(_ticket())


@pytest.fixture
def admin_client(client, session, monkeypatch):
    """`client` (an authed caller) with the admin gate opened and DB faked."""
    from app.api import app

    async def _fake_get_db():
        yield session

    app.dependency_overrides[verify_admin_access] = lambda: None
    app.dependency_overrides[get_db] = _fake_get_db
    yield client


@pytest.fixture
def paginate(monkeypatch):
    fake = AsyncMock(return_value=Page(items=[], total=0, page=1, size=50, pages=0))
    monkeypatch.setattr("app.services.support_service.paginate", fake)
    return fake


# --- The queue --------------------------------------------------------------


def test_the_queue_is_paginated_newest_activity_first(admin_client, paginate):
    response = admin_client.get("/admin/support/tickets?page=2&size=10")

    assert response.status_code == 200
    assert set(response.json()) == {"items", "total", "page", "size", "pages"}
    params = paginate.call_args.kwargs["params"]
    assert (params.page, params.size) == (2, 10)


@pytest.mark.parametrize(
    "query,expected",
    [
        pytest.param("status=open", "support_ticket.status = 'open'", id="status"),
        pytest.param(
            "category=billing", "support_ticket.category = 'billing'", id="category"
        ),
        pytest.param(f"pubkey={OWNER}", f"'{OWNER}'", id="pubkey"),
    ],
)
def test_the_queue_filters(admin_client, paginate, query, expected):
    admin_client.get(f"/admin/support/tickets?{query}")

    _, stmt = paginate.call_args.args
    assert expected in _sql(stmt)


def test_an_unfiltered_queue_constrains_nothing(admin_client, paginate):
    admin_client.get("/admin/support/tickets")

    _, stmt = paginate.call_args.args
    assert "WHERE" not in _sql(stmt)


def test_queue_rows_say_who_the_ticket_is_from():
    # An admin should know who they are talking to before typing.
    from app.schemas.schemas import AdminSupportTicketItem

    row = AdminSupportTicketItem.model_validate(_ticket())

    assert (row.pubkey, row.notify_email) == (OWNER, "someone@example.com")


def test_the_queue_is_ordered_by_most_recent_activity():
    sql = _sql(build_admin_support_tickets_stmt(None, None, None))

    assert "ORDER BY support_ticket.last_message_at DESC" in sql


# --- Reading any thread -----------------------------------------------------


def test_an_admin_opens_a_thread_that_is_not_theirs(admin_client, session):
    response = admin_client.get("/admin/support/tickets/7")

    assert response.status_code == 200
    assert response.json()["ticket"]["id"] == 7
    assert response.json()["requester"]["pubkey"] == OWNER


def test_an_admin_thread_says_which_human_replied(admin_client, session):
    message = SupportMessage(
        ticket_id=7, author="support", body="Looking into it", actor_pubkey=ADMIN
    )
    message.id, message.created_at = 1, datetime(2026, 9, 23, 12, 0, 0)
    session.execute = _thread_with(session, [message], [])

    body = admin_client.get("/admin/support/tickets/7").json()

    assert body["messages"][0]["actor_pubkey"] == ADMIN


def _thread_with(session, messages, events):
    async def execute(stmt):
        session.statements.append(stmt)
        sql = _sql(stmt)
        if "FROM support_message" in sql:
            return _ScalarsResult(messages)
        if "FROM support_event" in sql:
            return _ScalarsResult(events)
        return _Result(session.ticket)

    return execute


def test_an_unknown_ticket_is_not_found(admin_client, session):
    session.ticket = None

    assert admin_client.get("/admin/support/tickets/7").status_code == 404


# --- Answering --------------------------------------------------------------


def test_a_reply_answers_the_ticket(admin_client, session):
    response = admin_client.post(
        "/admin/support/tickets/7/messages", json={"body": "We found it"}
    )

    assert response.status_code == 200
    assert session.ticket.status == "answered"
    assert session.ticket.last_message_author == "support"
    assert session.of(SupportEvent) == []


def test_a_reply_is_attributed_to_the_admin_who_wrote_it(admin_client, session, caller):
    admin_client.post("/admin/support/tickets/7/messages", json={"body": "We found it"})

    message = session.of(SupportMessage)[0]
    assert (message.author, message.actor_pubkey) == ("support", caller.pubkey)


def test_a_reply_to_a_closed_ticket_reopens_it(admin_client, session):
    session.ticket = _ticket(status="closed", closed_at=datetime(2026, 9, 1, 9, 0))

    response = admin_client.post(
        "/admin/support/tickets/7/messages", json={"body": "One more thing"}
    )

    assert response.status_code == 200
    assert session.ticket.status == "answered"
    assert session.ticket.closed_at is None
    assert [(e.type, e.actor) for e in session.of(SupportEvent)] == [
        ("reopened", "support")
    ]


def test_an_explicit_reopen_takes_no_message(admin_client, session):
    session.ticket = _ticket(status="closed", closed_at=datetime(2026, 9, 1, 9, 0))

    response = admin_client.post("/admin/support/tickets/7/reopen")

    assert response.status_code == 200
    assert session.ticket.status == "open"
    assert session.of(SupportMessage) == []
    assert [(e.type, e.actor) for e in session.of(SupportEvent)] == [
        ("reopened", "support")
    ]


def test_reopening_an_open_ticket_changes_nothing(admin_client, session):
    response = admin_client.post("/admin/support/tickets/7/reopen")

    assert response.status_code == 200
    assert session.of(SupportEvent) == []


def test_closing_without_a_message_appends_none(admin_client, session):
    response = admin_client.post("/admin/support/tickets/7/close", json={})

    assert response.status_code == 200
    assert session.ticket.status == "closed"
    assert session.of(SupportMessage) == []
    assert [(e.type, e.actor) for e in session.of(SupportEvent)] == [
        ("closed", "support")
    ]


def test_closing_with_a_message_appends_it_first(admin_client, session):
    admin_client.post(
        "/admin/support/tickets/7/close", json={"message": "Marking this resolved"}
    )

    message = session.of(SupportMessage)[0]
    assert (message.author, message.body) == ("support", "Marking this resolved")
    # The message belongs to the thread, so it lands before the ticket closes.
    assert session.added.index(message) < session.added.index(
        session.of(SupportEvent)[0]
    )
    assert session.ticket.status == "closed"


def test_recategorizing_records_an_event(admin_client, session):
    response = admin_client.patch(
        "/admin/support/tickets/7", json={"category": "billing"}
    )

    assert response.status_code == 200
    assert session.ticket.category == "billing"
    assert [(e.type, e.actor) for e in session.of(SupportEvent)] == [
        ("recategorized", "support")
    ]


def test_recategorizing_to_the_same_category_records_nothing(admin_client, session):
    response = admin_client.patch(
        "/admin/support/tickets/7", json={"category": "scores"}
    )

    assert response.status_code == 200
    assert session.of(SupportEvent) == []


def test_answering_locks_the_ticket_row(admin_client, session):
    admin_client.post("/admin/support/tickets/7/messages", json={"body": "hi"})

    assert any("FOR UPDATE" in _sql(s) for s in session.statements)


# --- The gate ---------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,payload",
    [
        pytest.param("get", "/admin/support/tickets", None, id="list"),
        pytest.param("get", "/admin/support/tickets/7", None, id="thread"),
        pytest.param(
            "post", "/admin/support/tickets/7/messages", {"body": "x"}, id="reply"
        ),
        pytest.param("post", "/admin/support/tickets/7/reopen", None, id="reopen"),
        pytest.param("post", "/admin/support/tickets/7/close", {}, id="close"),
        pytest.param(
            "patch", "/admin/support/tickets/7", {"category": "bug"}, id="recategorize"
        ),
    ],
)
def test_a_non_admin_is_refused(client, session, method, path, payload):
    # No `verify_admin_access` override: the real gate runs.
    from app.api import app

    async def _fake_get_db():
        yield session

    app.dependency_overrides[get_db] = _fake_get_db

    response = getattr(client, method)(path, **({"json": payload} if payload else {}))

    assert response.status_code == 403
    assert session.added == []
