"""Where outbound support notifications will plug in. Nothing is plugged in.

No email, no Nostr DM, no webhook ships with this. What ships is the shape of a
notification and the places one is raised, so adding a transport later is one
file and one line rather than a hunt for the right call sites.

A notification carries **no ticket content** — an identifier, a category and who
it is for, never a subject or a body. That is enforced by the type rather than
left to each transport to remember: a team chat channel is usually a wider
audience than the people entitled to read tickets, and an inbox is not behind
the login at all. A transport that wants the content can read the ticket.
"""

import asyncio
import enum
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr

logger = loggr.get_logger(__name__)


class SupportNotificationKind(str, enum.Enum):
    TICKET_OPENED = "ticket_opened"
    USER_REPLIED = "user_replied"
    # Both support-side moments — answering, and closing with a note — are the
    # same signal to the user: support said something, go and read it.
    SUPPORT_REPLIED = "support_replied"


@dataclass(frozen=True)
class SupportNotification:
    kind: SupportNotificationKind
    audience: Literal["team", "user"]
    ticket_id: int
    category: str
    # Where a transport could reach the requester. Null on a team notification,
    # and null on a user one when they left no address.
    recipient_pubkey: str | None
    recipient_email: str | None


Sink = Callable[[SupportNotification], Awaitable[None]]

_SINKS: list[Sink] = []
# Strong references to in-flight fan-outs: a bare `create_task` result can be
# garbage collected before it runs.
_PENDING: set[asyncio.Task] = set()


def register_sink(sink: Sink) -> None:
    """Add a transport. Called at startup, gated on its own configuration."""
    _SINKS.append(sink)


def clear_sinks() -> None:
    _SINKS.clear()


def notify(notification: SupportNotification) -> None:
    """Raise a notification. Never raises, never blocks, and does nothing at
    all when no transport is registered."""
    if not _SINKS:
        return
    task = asyncio.create_task(_fan_out(notification))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


def notify_after_commit(db: AsyncDBSession, notification: SupportNotification) -> None:
    """Raise it only once the work that caused it is actually committed.

    `get_db` commits after the handler returns, and a task scheduled before
    then can run during that very commit — so raising inline risks telling a
    transport about a ticket whose transaction then rolled back. Hanging it on
    the session's `after_commit` means no commit, no notification.
    """
    if not _SINKS:
        return

    @event.listens_for(db.sync_session, "after_commit", once=True)
    def _fire(_session) -> None:
        notify(notification)


# Long enough for a transport to finish, short enough that a hung one cannot
# hold a deploy open. A dropped content-free nudge costs a delay, not data.
DRAIN_TIMEOUT_SECONDS = 5.0


async def drain_notifications(timeout: float = DRAIN_TIMEOUT_SECONDS) -> int:
    """Await the in-flight fan-outs, but never indefinitely.

    Called on shutdown, so an unbounded wait would let one hung sink — an SMTP
    connection to a dead host with no socket timeout — block the process from
    exiting. Whatever has not finished by then is abandoned.
    """
    pending = list(_PENDING)
    if not pending:
        return 0
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout
        )
    except asyncio.TimeoutError:
        logger.error(
            f"{sum(not t.done() for t in pending)} support notification(s) "
            f"abandoned after {timeout}s"
        )
    return len(pending)


async def _fan_out(notification: SupportNotification) -> None:
    for sink in list(_SINKS):
        try:
            await sink(notification)
        except Exception as e:
            # A dead transport must not turn an admin's reply into an error.
            logger.error(f"Support notification sink failed: {e}")
