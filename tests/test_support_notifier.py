"""The place outbound notifications plug into, with nothing plugged in.

No transport ships. What ships is the shape of a notification and the promise
that raising one cannot fail or block the request that caused it.
"""

import asyncio
import dataclasses

import pytest

from app.services.support_notifier import (
    SupportNotification,
    SupportNotificationKind,
    clear_sinks,
    drain_notifications,
    notify,
    register_sink,
)


@pytest.fixture(autouse=True)
def no_sinks():
    clear_sinks()
    yield
    clear_sinks()


def _notification(**overrides) -> SupportNotification:
    return dataclasses.replace(
        SupportNotification(
            kind=SupportNotificationKind.TICKET_OPENED,
            audience="team",
            ticket_id=7,
            category="scores",
            recipient_pubkey=None,
            recipient_email=None,
        ),
        **overrides,
    )


def test_a_notification_cannot_carry_ticket_content():
    # Structural, not a convention each transport has to remember: a team chat
    # channel is a wider audience than the people entitled to read tickets, and
    # an inbox is not behind the login at all.
    fields = {f.name for f in dataclasses.fields(SupportNotification)}

    assert fields == {
        "kind",
        "audience",
        "ticket_id",
        "category",
        "recipient_pubkey",
        "recipient_email",
    }
    for forbidden in ("subject", "body", "message", "diagnostics", "notify_email"):
        assert forbidden not in fields


def test_a_notification_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _notification().ticket_id = 9  # type: ignore[misc]


def test_with_no_transport_raising_one_does_nothing():
    async def _run():
        notify(_notification())

        assert await drain_notifications() == 0

    asyncio.run(_run())


def test_a_registered_transport_receives_it():
    async def _run():
        seen: list[SupportNotification] = []

        async def sink(n):
            seen.append(n)

        register_sink(sink)
        notify(_notification(ticket_id=11))
        await drain_notifications()

        assert [n.ticket_id for n in seen] == [11]

    asyncio.run(_run())


def test_a_transport_that_throws_is_swallowed_and_the_others_still_run():
    async def _run():
        reached: list[str] = []

        async def broken(n):
            raise RuntimeError("mail server is down")

        async def working(n):
            reached.append("working")

        register_sink(broken)
        register_sink(working)
        notify(_notification())
        await drain_notifications()

        # A dead mail server must not turn an admin's reply into an error.
        assert reached == ["working"]

    asyncio.run(_run())


def test_raising_one_does_not_block_the_caller():
    async def _run():
        released = asyncio.Event()

        async def slow(n):
            await released.wait()

        register_sink(slow)
        notify(_notification())  # returns immediately despite the sink hanging

        released.set()
        await drain_notifications()

    asyncio.run(_run())


def test_a_pending_notification_is_held_against_collection():
    async def _run():
        # A bare create_task with no reference can be garbage collected mid-flight.
        started = asyncio.Event()

        async def sink(n):
            started.set()

        register_sink(sink)
        notify(_notification())

        assert await drain_notifications() == 1
        assert started.is_set()

    asyncio.run(_run())


def test_a_notification_waits_for_the_commit():
    """`get_db` commits after the handler returns, so raising inline can tell a
    transport about work that then rolled back."""

    async def _run():
        from types import SimpleNamespace

        from sqlalchemy.orm import Session

        from app.services.support_notifier import notify_after_commit

        fired: list = []

        async def sink(n):
            fired.append(n)

        register_sink(sink)
        sync_session = Session()
        notify_after_commit(SimpleNamespace(sync_session=sync_session), _notification())

        await drain_notifications()
        assert fired == [], "raised before the transaction committed"

        sync_session.dispatch.after_commit(sync_session)
        await drain_notifications()
        assert len(fired) == 1

    asyncio.run(_run())


def test_nothing_is_hung_on_the_session_when_no_transport_is_registered():
    from types import SimpleNamespace

    from sqlalchemy.orm import Session

    from app.services.support_notifier import notify_after_commit

    sync_session = Session()
    notify_after_commit(SimpleNamespace(sync_session=sync_session), _notification())

    # No sinks: no listener, so nothing accumulates on long-lived sessions.
    sync_session.dispatch.after_commit(sync_session)
