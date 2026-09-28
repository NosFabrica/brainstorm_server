"""Diagnostics expiry, executed against a real Postgres.

`tests/test_support_diagnostics_expiry.py` matches the compiled SQL, which
proves the predicate's shape and nothing about its effect. Only a real row
shows that the ticket, its messages and its events survive the sweep — the
promise the whole feature rests on, since expiring a snapshot is worth nothing
if it takes the conversation with it.

Requires the local Postgres (e.g. ``docker compose up -d``). Run with::

    poetry run pytest tests/integration -m integration
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.database import db_session, engine
from app.repos.support_repo import clear_expired_support_diagnostics_on_db
from app.utils.datetimes import utc_now

pytestmark = pytest.mark.integration

PUBKEY = "e" * 64
RETENTION = timedelta(days=30)


async def _cleanup(db) -> None:
    # support_message / support_event are ON DELETE CASCADE from the ticket.
    await db.execute(
        text("DELETE FROM support_ticket WHERE pubkey = :p"), {"p": PUBKEY}
    )


async def _ticket(db, *, age_days: int, diagnostics: str | None = '{"App": "v1"}'):
    filed = utc_now() - timedelta(days=age_days)
    row = (
        await db.execute(
            text(
                """
                INSERT INTO support_ticket
                    (pubkey, subject, category, status, notify_email, diagnostics,
                     last_message_at, last_message_author, created_at, updated_at)
                VALUES
                    (:p, 'Scores look wrong', 'scores', 'open', 'x@example.com',
                     CAST(:d AS jsonb), :t, 'user', :t, :t)
                RETURNING id
                """
            ),
            {"p": PUBKEY, "d": diagnostics, "t": filed},
        )
    ).first()
    ticket_id = row[0]
    await db.execute(
        text(
            "INSERT INTO support_message (ticket_id, author, body, created_at)"
            " VALUES (:i, 'user', 'My scores are stale', :t)"
        ),
        {"i": ticket_id, "t": filed},
    )
    await db.execute(
        text(
            "INSERT INTO support_event (ticket_id, type, actor, at)"
            " VALUES (:i, 'opened', 'user', :t)"
        ),
        {"i": ticket_id, "t": filed},
    )
    return ticket_id


def _drive(run) -> None:
    # asyncpg connections are bound to the loop that opened them; dispose in-loop.
    async def _go():
        try:
            await run()
        finally:
            await engine.dispose()

    asyncio.run(_go())


def test_the_sweep_takes_the_snapshot_and_leaves_the_conversation():
    async def _run():
        async with db_session() as db:
            await _cleanup(db)
            ticket_id = await _ticket(db, age_days=31)

            cleared = await clear_expired_support_diagnostics_on_db(db, RETENTION)

            assert cleared == 1
            ticket = (
                await db.execute(
                    text(
                        "SELECT diagnostics, subject, category, status, notify_email,"
                        " last_message_author FROM support_ticket WHERE id = :i"
                    ),
                    {"i": ticket_id},
                )
            ).first()
            assert ticket is not None, "the sweep deleted the ticket"
            assert ticket[0] is None, "the snapshot survived"
            assert tuple(ticket[1:]) == (
                "Scores look wrong",
                "scores",
                "open",
                "x@example.com",
                "user",
            )

            messages = (
                await db.execute(
                    text("SELECT body FROM support_message WHERE ticket_id = :i"),
                    {"i": ticket_id},
                )
            ).all()
            events = (
                await db.execute(
                    text("SELECT type FROM support_event WHERE ticket_id = :i"),
                    {"i": ticket_id},
                )
            ).all()
            assert [m[0] for m in messages] == ["My scores are stale"]
            assert [e[0] for e in events] == ["opened"]

            await _cleanup(db)

    _drive(_run)


def test_a_snapshot_inside_the_window_is_left_alone():
    async def _run():
        async with db_session() as db:
            await _cleanup(db)
            ticket_id = await _ticket(db, age_days=29)

            cleared = await clear_expired_support_diagnostics_on_db(db, RETENTION)

            assert cleared == 0
            kept = (
                await db.execute(
                    text("SELECT diagnostics FROM support_ticket WHERE id = :i"),
                    {"i": ticket_id},
                )
            ).scalar_one()
            assert kept == {"App": "v1"}

            await _cleanup(db)

    _drive(_run)


def test_a_second_sweep_rewrites_nothing():
    async def _run():
        async with db_session() as db:
            await _cleanup(db)
            await _ticket(db, age_days=31)

            assert await clear_expired_support_diagnostics_on_db(db, RETENTION) == 1
            # Without the `diagnostics IS NOT NULL` guard this would keep matching
            # every old row, forever, on every sweep.
            assert await clear_expired_support_diagnostics_on_db(db, RETENTION) == 0

            await _cleanup(db)

    _drive(_run)
