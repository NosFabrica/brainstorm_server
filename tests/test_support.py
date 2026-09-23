"""User-facing support surface: the support state and filing a ticket.

Router + service + repo run for real; `get_db` yields a mock session that
records every statement it is handed, and the ticket-list `paginate` is patched
at the service's import site so the listing never needs a real database.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from fastapi_pagination import Page
from sqlalchemy.dialects import postgresql

from app.core.database import get_db
from app.db_models import SupportEvent, SupportMessage, SupportTicket
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

    def scalar_one(self):
        return self._scalar

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


# --- Filing a ticket (`POST /user/support/tickets`) -------------------------


class _FilingSession:
    """Answers the policy lookup and the open-ticket count; records every add.

    `flush` stands in for the database's server defaults: ids, and `now()` on
    the timestamp columns the models leave to the server.
    """

    def __init__(self, policy: SimpleNamespace, open_count: int = 0) -> None:
        self.policy = policy
        self.open_count = open_count
        self.statements: list = []
        self.added: list = []
        self._next_id = 1

    async def execute(self, stmt):
        self.statements.append(stmt)
        sql = _sql(stmt)
        if "brainstorm_nsec" in sql:
            return _Result(1)
        if "pg_advisory_xact_lock" in sql:
            return _Result(None)
        if "count(" in sql:
            return _Result(self.open_count)
        return _Result(self.policy)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        now = datetime(2026, 9, 23, 12, 0, 0)
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1
            for column in ("created_at", "updated_at", "at"):
                if hasattr(type(obj), column) and getattr(obj, column) is None:
                    setattr(obj, column, now)

    async def refresh(self, obj) -> None:
        pass

    def of(self, model) -> list:
        return [o for o in self.added if isinstance(o, model)]


@pytest.fixture
def filing_session():
    return _FilingSession(_policy(True))


@pytest.fixture
def filing_client(client, filing_session):
    from app.api import app

    async def _fake_get_db():
        yield filing_session

    app.dependency_overrides[get_db] = _fake_get_db
    yield client


_TICKET = {
    "subject": "Scores stuck",
    "body": "Nothing since Monday.",
    "category": "scores",
}


def test_filing_records_the_ticket_its_first_message_and_an_opened_event(
    filing_client, filing_session, caller
):
    response = filing_client.post("/user/support/tickets", json=_TICKET)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["subject"] == "Scores stuck"
    assert data["category"] == "scores"
    assert data["status"] == "open"
    assert data["last_message_author"] == "user"
    assert data["closed_at"] is None

    [ticket] = filing_session.of(SupportTicket)
    assert ticket.pubkey == caller.pubkey
    assert data["id"] == ticket.id
    [message] = filing_session.of(SupportMessage)
    assert (message.ticket_id, message.author, message.body) == (
        ticket.id,
        "user",
        "Nothing since Monday.",
    )
    assert ticket.last_message_at == message.created_at
    [event] = filing_session.of(SupportEvent)
    assert (event.ticket_id, event.type, event.actor) == (ticket.id, "opened", "user")


def test_filing_is_refused_when_support_is_not_included(filing_client, filing_session):
    filing_session.policy = _policy(False)

    response = filing_client.post("/user/support/tickets", json=_TICKET)

    assert response.status_code == 403
    assert isinstance(response.json()["detail"], str)
    assert filing_session.added == []


def test_whitelisted_pubkey_may_file_without_the_policy(
    filing_client, filing_session, caller, monkeypatch
):
    filing_session.policy = _policy(False)
    monkeypatch.setattr(
        "app.services.support_entitlement.get_whitelisted_pubkeys",
        lambda: {caller.pubkey},
    )

    response = filing_client.post("/user/support/tickets", json=_TICKET)

    assert response.status_code == 200


def test_category_is_required(filing_client):
    body = {k: v for k, v in _TICKET.items() if k != "category"}

    response = filing_client.post("/user/support/tickets", json=body)

    assert response.status_code == 422


def test_an_unrecognised_category_is_stored_verbatim(filing_client, filing_session):
    response = filing_client.post(
        "/user/support/tickets", json={**_TICKET, "category": "Brand-New Thing"}
    )

    assert response.status_code == 200
    assert response.json()["data"]["category"] == "Brand-New Thing"
    [ticket] = filing_session.of(SupportTicket)
    assert ticket.category == "Brand-New Thing"


def test_past_the_cap_filing_is_refused_with_a_plain_string(
    filing_client, filing_session
):
    filing_session.open_count = 5

    response = filing_client.post("/user/support/tickets", json=_TICKET)

    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)
    assert filing_session.added == []


def test_just_under_the_cap_filing_goes_through(filing_client, filing_session):
    filing_session.open_count = 4

    response = filing_client.post("/user/support/tickets", json=_TICKET)

    assert response.status_code == 200


def test_the_cap_counts_every_unclosed_ticket_of_the_caller(
    filing_client, filing_session, caller
):
    filing_client.post("/user/support/tickets", json=_TICKET)

    [count_sql] = [_sql(s) for s in filing_session.statements if "count(" in _sql(s)]
    assert f"support_ticket.pubkey = '{caller.pubkey}'" in count_sql
    assert "support_ticket.status != 'closed'" in count_sql


def test_the_cap_is_counted_under_a_per_caller_lock(
    filing_client, filing_session, caller
):
    # Two filings at once must not both see 4 and leave 6 unclosed.
    filing_client.post("/user/support/tickets", json=_TICKET)

    sqls = [_sql(s) for s in filing_session.statements]
    [lock] = [i for i, sql in enumerate(sqls) if "pg_advisory_xact_lock" in sql]
    [count] = [i for i, sql in enumerate(sqls) if "count(" in sql]
    assert lock < count
    assert caller.pubkey in sqls[lock]


def test_email_and_diagnostics_are_stored_as_given(filing_client, filing_session):
    diagnostics = {"App version": "1.4.2", "Browser": "Firefox 131"}

    response = filing_client.post(
        "/user/support/tickets",
        json={**_TICKET, "notify_email": "me@example.com", "diagnostics": diagnostics},
    )

    assert response.status_code == 200
    [ticket] = filing_session.of(SupportTicket)
    assert ticket.notify_email == "me@example.com"
    assert ticket.diagnostics == diagnostics


def test_email_is_stored_exactly_as_typed(filing_client, filing_session):
    filing_client.post(
        "/user/support/tickets", json={**_TICKET, "notify_email": "Me@Example.COM"}
    )

    [ticket] = filing_session.of(SupportTicket)
    assert ticket.notify_email == "Me@Example.COM"


def test_email_and_diagnostics_are_optional(filing_client, filing_session):
    response = filing_client.post("/user/support/tickets", json=_TICKET)

    assert response.status_code == 200
    [ticket] = filing_session.of(SupportTicket)
    assert ticket.notify_email is None
    assert ticket.diagnostics is None


def test_a_malformed_email_is_rejected(filing_client):
    response = filing_client.post(
        "/user/support/tickets", json={**_TICKET, "notify_email": "not-an-email"}
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "diagnostics",
    [
        pytest.param({"blob": "x" * 20_000}, id="oversized"),
        pytest.param({f"k{i}": "v" for i in range(51)}, id="too-many-keys"),
        pytest.param({"k" * 200: "v"}, id="absurd-key"),
        pytest.param({"nested": {"a": "b"}}, id="not-flat"),
    ],
)
def test_bad_diagnostics_are_rejected(filing_client, filing_session, diagnostics):
    response = filing_client.post(
        "/user/support/tickets", json={**_TICKET, "diagnostics": diagnostics}
    )

    assert response.status_code == 422
    assert filing_session.added == []


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"subject": "   "}, id="blank-subject"),
        pytest.param({"subject": "s" * 201}, id="long-subject"),
        pytest.param({"body": ""}, id="empty-body"),
        pytest.param({"body": "b" * 10_001}, id="long-body"),
        pytest.param({"category": ""}, id="empty-category"),
        pytest.param({"category": "c" * 65}, id="long-category"),
    ],
)
def test_out_of_bounds_fields_are_rejected(filing_client, override):
    response = filing_client.post("/user/support/tickets", json={**_TICKET, **override})

    assert response.status_code == 422


def test_filing_never_reads_billing_tables(filing_client, filing_session):
    filing_client.post("/user/support/tickets", json=_TICKET)

    sql = " ".join(_sql(s) for s in filing_session.statements)
    assert "billing_plan" not in sql
    assert "user_subscription" not in sql


# --- Reading a thread (`GET /user/support/tickets/{id}`) --------------------


class _ThreadSession:
    """Answers the ticket lookup and its two child selects.

    `ticket` None stands for a ticket that does not exist; a ticket whose
    `pubkey` is somebody else's stands for one the caller may not read. Both
    must look the same from outside.
    """

    def __init__(
        self,
        ticket: SupportTicket | None,
        messages: list[SupportMessage] | None = None,
        events: list[SupportEvent] | None = None,
    ) -> None:
        self.ticket = ticket
        self.messages = messages or []
        self.events = events or []
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        sql = _sql(stmt)
        if "FROM support_message" in sql:
            return _ScalarsResult(self.messages)
        if "FROM support_event" in sql:
            return _ScalarsResult(self.events)
        return _Result(self.ticket)


class _ScalarsResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


def _ticket_row(pubkey: str, **overrides) -> SupportTicket:
    now = datetime(2026, 9, 23, 12, 0, 0)
    row = SupportTicket(
        pubkey=pubkey,
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


def _thread_client(client, session):
    from app.api import app

    async def _fake_get_db():
        yield session

    app.dependency_overrides[get_db] = _fake_get_db
    return client


def test_a_thread_carries_its_messages_events_diagnostics_and_requester(
    client, caller
):
    now = datetime(2026, 9, 23, 12, 0, 0)
    message = SupportMessage(
        ticket_id=7, author="user", body="My scores are stale", actor_pubkey=None
    )
    message.id, message.created_at = 1, now
    event = SupportEvent(ticket_id=7, type="opened", actor="user", actor_pubkey=None)
    event.id, event.at = 1, now
    session = _ThreadSession(_ticket_row(caller.pubkey), [message], [event])

    response = _thread_client(client, session).get("/user/support/tickets/7")

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == {"ticket", "messages", "events", "diagnostics", "requester"}
    assert data["ticket"]["id"] == 7
    assert data["messages"] == [
        {
            "id": 1,
            "author": "user",
            "body": "My scores are stale",
            "created_at": now.isoformat(),
        }
    ]
    assert data["events"] == [{"type": "opened", "at": now.isoformat(), "by": "user"}]
    assert data["diagnostics"] == {"App": "v0.1.0-alpha"}
    assert data["requester"] == {
        "pubkey": caller.pubkey,
        "notify_email": "someone@example.com",
    }


def test_a_thread_never_exposes_which_human_replied(client, caller):
    now = datetime(2026, 9, 23, 12, 0, 0)
    message = SupportMessage(
        ticket_id=7, author="support", body="Looking into it", actor_pubkey="a" * 64
    )
    message.id, message.created_at = 1, now
    event = SupportEvent(
        ticket_id=7, type="reopened", actor="support", actor_pubkey="a" * 64
    )
    event.id, event.at = 1, now
    session = _ThreadSession(_ticket_row(caller.pubkey), [message], [event])

    body = _thread_client(client, session).get("/user/support/tickets/7").text

    assert "a" * 64 not in body
    assert "actor_pubkey" not in body


def test_somebody_elses_ticket_is_not_found_rather_than_forbidden(client):
    session = _ThreadSession(_ticket_row("f" * 64))

    response = _thread_client(client, session).get("/user/support/tickets/7")

    # 403 would confirm the ticket exists.
    assert response.status_code == 404
    assert session.statements, "the route never ran — a 404 from the router itself"


def test_a_ticket_that_does_not_exist_is_not_found(client):
    session = _ThreadSession(None)

    response = _thread_client(client, session).get("/user/support/tickets/7")

    assert response.status_code == 404
    assert session.statements, "the route never ran — a 404 from the router itself"


def test_a_thread_is_readable_when_support_is_not_included(client, caller):
    # Entitlement gates writing only: a lapsed subscriber keeps their answers.
    session = _ThreadSession(_ticket_row(caller.pubkey))

    response = _thread_client(client, session).get("/user/support/tickets/7")

    assert response.status_code == 200
    sql = " ".join(_sql(s) for s in session.statements)
    assert "scheduling" not in sql


def test_a_thread_reads_in_a_stable_order(client, caller):
    session = _ThreadSession(_ticket_row(caller.pubkey))

    _thread_client(client, session).get("/user/support/tickets/7")

    # `created_at` ties when two rows land in the same millisecond.
    ordered = [_sql(s) for s in session.statements if "ORDER BY" in _sql(s)]
    assert len(ordered) == 2
    assert all(sql.rstrip().endswith(".id") for sql in ordered)


# --- Replying and resolving -------------------------------------------------


class _WriteSession(_FilingSession):
    """Adds the locked ticket read that the reply/resolve paths open with."""

    def __init__(self, ticket: SupportTicket | None) -> None:
        super().__init__(_policy(True))
        self.ticket = ticket
        self._next_id = 100

    async def execute(self, stmt):
        self.statements.append(stmt)
        if "FOR UPDATE" in _sql(stmt):
            return _Result(self.ticket)
        return await super().execute(stmt)


@pytest.fixture
def no_message_rate_limit(monkeypatch):
    limiter = AsyncMock()
    monkeypatch.setattr(
        "app.routers.support.router.validate_support_message_allowed", limiter
    )
    return limiter


def _write_client(client, session):
    from app.api import app

    async def _fake_get_db():
        yield session

    app.dependency_overrides[get_db] = _fake_get_db
    return client


def _reply(client, session, body="Still broken"):
    return _write_client(client, session).post(
        "/user/support/tickets/7/messages", json={"body": body}
    )


def test_a_reply_puts_an_answered_ticket_back_in_supports_court(
    client, caller, no_message_rate_limit
):
    session = _WriteSession(_ticket_row(caller.pubkey, status="answered"))

    response = _reply(client, session)

    assert response.status_code == 200
    assert session.ticket.status == "open"
    # The message is the event; a shuffle between open states records none.
    assert session.of(SupportEvent) == []


def test_a_reply_to_an_open_ticket_records_no_event(
    client, caller, no_message_rate_limit
):
    session = _WriteSession(_ticket_row(caller.pubkey, status="open"))

    _reply(client, session)

    assert session.ticket.status == "open"
    assert session.of(SupportEvent) == []


def test_a_reply_to_a_closed_ticket_reopens_it(client, caller, no_message_rate_limit):
    closed = _ticket_row(
        caller.pubkey, status="closed", closed_at=datetime(2026, 9, 1, 9, 0, 0)
    )
    session = _WriteSession(closed)

    response = _reply(client, session)

    assert response.status_code == 200
    assert session.ticket.status == "open"
    assert session.ticket.closed_at is None
    events = session.of(SupportEvent)
    assert [(e.type, e.actor) for e in events] == [("reopened", "user")]


def test_a_reply_moves_the_tickets_last_activity(
    client, caller, no_message_rate_limit
):
    session = _WriteSession(_ticket_row(caller.pubkey, status="answered"))

    _reply(client, session)

    message = session.of(SupportMessage)[0]
    assert session.ticket.last_message_at == message.created_at
    assert session.ticket.last_message_author == "user"


def test_a_reply_is_recorded_as_the_users_own(client, caller, no_message_rate_limit):
    session = _WriteSession(_ticket_row(caller.pubkey))

    body = _reply(client, session, "Still broken").json()["data"]

    assert (body["author"], body["body"]) == ("user", "Still broken")
    assert session.of(SupportMessage)[0].actor_pubkey == caller.pubkey


def test_replying_locks_the_ticket_row(client, caller, no_message_rate_limit):
    session = _WriteSession(_ticket_row(caller.pubkey))

    _reply(client, session)

    # Read-modify-write: without the lock a concurrent admin reply can
    # double-record an event or leave last-activity on the older message.
    assert any("FOR UPDATE" in _sql(s) for s in session.statements)


def test_replying_is_rate_limited_per_pubkey(client, caller, no_message_rate_limit):
    session = _WriteSession(_ticket_row(caller.pubkey))

    _reply(client, session)

    no_message_rate_limit.assert_awaited_once_with(caller.pubkey)


def test_a_reply_to_somebody_elses_ticket_is_not_found(
    client, no_message_rate_limit
):
    session = _WriteSession(_ticket_row("f" * 64))

    assert _reply(client, session).status_code == 404


@pytest.mark.parametrize(
    "body", [pytest.param("", id="empty"), pytest.param("b" * 10_001, id="too-long")]
)
def test_an_out_of_bounds_reply_is_rejected(
    client, caller, no_message_rate_limit, body
):
    session = _WriteSession(_ticket_row(caller.pubkey))

    assert _reply(client, session, body).status_code == 422


def test_resolving_closes_the_ticket_and_says_who(client, caller):
    session = _WriteSession(_ticket_row(caller.pubkey, status="answered"))

    response = _write_client(client, session).post(
        "/user/support/tickets/7/resolve"
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "closed"
    assert session.ticket.closed_at is not None
    assert [(e.type, e.actor) for e in session.of(SupportEvent)] == [
        ("closed", "user")
    ]


def test_resolving_an_already_closed_ticket_records_one_closure(client, caller):
    session = _WriteSession(_ticket_row(caller.pubkey, status="closed"))

    response = _write_client(client, session).post(
        "/user/support/tickets/7/resolve"
    )

    assert response.status_code == 200
    assert session.of(SupportEvent) == []


def test_resolving_somebody_elses_ticket_is_not_found(client):
    session = _WriteSession(_ticket_row("f" * 64))

    response = _write_client(client, session).post(
        "/user/support/tickets/7/resolve"
    )

    assert response.status_code == 404


def test_a_lapsed_user_cannot_reply(client, caller, no_message_rate_limit):
    # Entitlement gates writing, and continuing a conversation is a write.
    session = _WriteSession(_ticket_row(caller.pubkey))
    session.policy = _policy(False)

    response = _reply(client, session)

    assert response.status_code == 403
    assert session.of(SupportMessage) == []


def test_a_lapsed_user_can_still_resolve_their_own_ticket(client, caller):
    # Closing a ticket you already own is not continuing a conversation.
    session = _WriteSession(_ticket_row(caller.pubkey, status="answered"))
    session.policy = _policy(False)

    response = _write_client(client, session).post("/user/support/tickets/7/resolve")

    assert response.status_code == 200
    assert session.ticket.status == "closed"


def test_closed_at_is_utc_not_the_hosts_local_clock(client, caller):
    from datetime import datetime, timezone

    session = _WriteSession(_ticket_row(caller.pubkey, status="answered"))

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    _write_client(client, session).post("/user/support/tickets/7/resolve")
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    # A local-clock `datetime.now()` fails this wherever TZ isn't UTC, and can
    # order `closed_at` before the `created_at` the database wrote.
    assert before <= session.ticket.closed_at <= after


# --- What reaches the notifier ---------------------------------------------


@pytest.fixture
def raised(monkeypatch):
    """Record what would be handed to a transport, without registering one."""
    seen: list = []
    monkeypatch.setattr(
        "app.services.support_service.notify_after_commit",
        lambda db, n: seen.append(n),
    )
    return seen


def test_filing_a_ticket_tells_the_team(filing_client, raised):
    filing_client.post("/user/support/tickets", json=_TICKET)

    assert [(n.kind.value, n.audience) for n in raised] == [("ticket_opened", "team")]


def test_a_user_reply_tells_the_team(client, caller, no_message_rate_limit, raised):
    session = _WriteSession(_ticket_row(caller.pubkey, status="answered"))

    _reply(client, session)

    assert [(n.kind.value, n.audience) for n in raised] == [("user_replied", "team")]


def test_resolving_tells_nobody(client, caller, raised):
    session = _WriteSession(_ticket_row(caller.pubkey, status="answered"))

    _write_client(client, session).post("/user/support/tickets/7/resolve")

    # Closing your own ticket is not news for anyone.
    assert raised == []


def test_a_team_notification_carries_no_way_to_reach_the_user(filing_client, raised):
    filing_client.post("/user/support/tickets", json=_TICKET)

    notification = raised[0]
    assert (notification.recipient_pubkey, notification.recipient_email) == (
        None,
        None,
    )
    assert notification.category == _TICKET["category"]
