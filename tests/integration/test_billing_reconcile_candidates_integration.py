"""The abandoned-checkout clock and who the sweep picks up, against real Postgres.

`tests/test_billing_repo.py` builds these statements but stubs the execute, so it
proves they name real model attributes and nothing more. Two things here are not
observable without a database: which rows the candidate query returns, and what
the `CASE` in `record_sync_failure_on_db` does to `sync_error_since` across
repeated failures. The second is the whole basis of the first — if the clock
restarts on every failure, nothing is ever abandoned; if it restarts on none, a
subscriber is written off for someone else's error.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.database import db_session, engine
from app.repos.user_subscription_repo import (
    AbandonRule,
    record_sync_failure_on_db,
    select_abandoned_checkouts_on_db,
    select_failing_syncs_on_db,
    select_reconcile_candidates_on_db,
    select_stale_syncs_on_db,
)
from app.services.billing_service import EntitlementReason

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 27, 12, 0, 0)
ABANDON_AFTER = timedelta(hours=24)
STALE_AFTER = timedelta(hours=6)
UNKNOWN = EntitlementReason.UNKNOWN_SUBSCRIPTION.value
ABANDONED_RULE = AbandonRule(after=ABANDON_AFTER, error=UNKNOWN)

LONG_AGO = NOW - timedelta(days=30)
RECENTLY = NOW - timedelta(hours=1)
READ_JUST_NOW = NOW - timedelta(minutes=5)
READ_LAST_MONTH = NOW - timedelta(days=30)

# Probe pubkeys, 64 hex like the real thing.
ABANDONED = "a" * 63 + "1"
STILL_YOUNG = "a" * 63 + "2"
GRANTED = "a" * 63 + "3"
OTHER_ERROR = "a" * 63 + "4"
CLOCK = "a" * 63 + "5"
PROBES = (ABANDONED, STILL_YOUNG, GRANTED, OTHER_ERROR, CLOCK)

# Kept apart from PROBES: those are all `pending`, which is what lets the
# cross-check below read "absent from the batch" as "abandoned". These are not.
SETTLED = "b" * 63 + "1"
SETTLED_GRANTED = "b" * 63 + "2"
SETTLED_ERRORED = "b" * 63 + "3"
PAUSED = "b" * 63 + "4"
CANCELED = "b" * 63 + "5"
SETTLED_PROBES = (SETTLED, SETTLED_GRANTED, SETTLED_ERRORED, PAUSED, CANCELED)
ALL_PROBES = PROBES + SETTLED_PROBES


async def _seed(
    db, pubkey, *, granted=None, error=None, since=None, status="pending", synced=None
):
    await db.execute(
        text(
            """
            INSERT INTO user_subscription
              (pubkey, flash_subscription_id, billing_plan_id,
               granted_scheduling_id, flash_status, last_sync_error,
               sync_error_since, last_synced_at, created_at, updated_at)
            VALUES (:pubkey, :sub, NULL, :granted, :status, :error,
                    :since, :synced, :long_ago, :long_ago)
            """
        ),
        {
            "pubkey": pubkey,
            "sub": f"probe-{pubkey[-1]}",
            "granted": granted,
            "status": status,
            "error": error,
            "since": since,
            "synced": synced if synced is not None else READ_JUST_NOW,
            "long_ago": LONG_AGO,
        },
    )


async def _row(db, pubkey):
    return (
        await db.execute(
            text(
                "SELECT last_sync_error, sync_error_since FROM user_subscription"
                " WHERE pubkey = :pubkey"
            ),
            {"pubkey": pubkey},
        )
    ).one()


async def _cleanup(db):
    await db.execute(
        text("DELETE FROM user_subscription WHERE pubkey = ANY(:keys)"),
        {"keys": list(ALL_PROBES)},
    )
    await db.commit()


def test_the_error_clock_runs_from_the_first_failure_of_its_kind():
    async def _run():
        try:
            async with db_session() as db:
                await _cleanup(db)
                await _seed(db, CLOCK)
                await db.commit()

                await record_sync_failure_on_db(db, CLOCK, UNKNOWN)
                await db.commit()
                _, started = await _row(db, CLOCK)
                assert started is not None, "the clock never started"

                # The same failure continuing must not look freshly minted, or
                # nothing ever ages past the window.
                await record_sync_failure_on_db(db, CLOCK, UNKNOWN)
                await db.commit()
                _, unchanged = await _row(db, CLOCK)
                assert unchanged == started, "a repeat restarted the clock"

                # A different failure is a different failure.
                await record_sync_failure_on_db(db, CLOCK, "vault down")
                await db.commit()
                reason, restarted = await _row(db, CLOCK)
                assert reason == "vault down"
                assert restarted > started, "a new reason kept the old clock"

                await _cleanup(db)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_an_abandoned_checkout_drops_out_and_everything_else_stays():
    async def _run():
        try:
            async with db_session() as db:
                await _cleanup(db)

                # Flash forgot it, nothing was ever granted, and it has said so
                # for longer than the window.
                await _seed(db, ABANDONED, error=UNKNOWN, since=LONG_AGO)
                # Same, but inside the window — one empty answer from Flash must
                # not strand a checkout somebody is still paying for.
                await _seed(db, STILL_YOUNG, error=UNKNOWN, since=RECENTLY)
                # Flash forgot a subscriber who holds a policy. A real anomaly;
                # keep asking however long it has been true.
                await _seed(db, GRANTED, granted=4, error=UNKNOWN, since=LONG_AGO)
                # We could not ask, rather than asked and got nothing.
                await _seed(db, OTHER_ERROR, error="vault down", since=LONG_AGO)
                await db.commit()

                rows = await select_reconcile_candidates_on_db(
                    db,
                    now=NOW,
                    stale_after=STALE_AFTER,
                    limit=500,
                    abandoned=ABANDONED_RULE,
                )
                selected = {row.pubkey for row in rows}

                assert ABANDONED not in selected, "abandoned checkout still swept"
                assert STILL_YOUNG in selected, "dropped a checkout still in its window"
                assert GRANTED in selected, "stopped watching a subscriber with a policy"
                assert OTHER_ERROR in selected, "an outage was treated as abandonment"

                # The sweep asks in SQL, the user-facing view asks in Python.
                # The two answering differently is how someone ends up written
                # off in one place and still "confirming your payment" in the
                # other, so they are checked against the same rows. Every probe
                # is `pending` and so matches the query's positive half — which
                # is what makes "absent from the batch" mean "abandoned" here.
                everything = (
                    await db.execute(
                        text(
                            "SELECT * FROM user_subscription WHERE pubkey = ANY(:keys)"
                        ),
                        {"keys": list(PROBES)},
                    )
                ).mappings()
                for row in everything:
                    in_python = ABANDONED_RULE.matches(SimpleNamespace(**row), NOW)
                    in_sql = row["pubkey"] not in selected
                    assert in_python == in_sql, (
                        f"{row['pubkey'][-1]}: python says abandoned={in_python}, "
                        f"sql says {in_sql}"
                    )

                await _cleanup(db)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_an_abandoned_checkout_leaves_the_alarming_sections_for_its_own():
    """The report exists so a genuine failure cannot hide among benign rows.

    Both sections it would otherwise sit in are bounded and unordered, and
    abandoned checkouts are the one failure that is both expected and unbounded
    in number — so they get counted, not mixed in.
    """
    async def _run():
        try:
            async with db_session() as db:
                await _cleanup(db)
                # Read long enough ago to be genuinely stale, so the assertion
                # below turns on the exclusion rather than on the clock.
                await _seed(
                    db, ABANDONED, error=UNKNOWN, since=LONG_AGO,
                    synced=READ_LAST_MONTH,
                )
                await _seed(
                    db, OTHER_ERROR, error="vault down", since=LONG_AGO,
                    synced=READ_LAST_MONTH,
                )
                await db.commit()

                failing = {
                    row.pubkey
                    for row in await select_failing_syncs_on_db(
                        db, limit=500, now=NOW, abandoned=ABANDONED_RULE
                    )
                }
                assert ABANDONED not in failing, "abandoned checkout crowds the failures"
                assert OTHER_ERROR in failing, "a real failure was hidden"

                # We stopped reading them, so their read clock is frozen by
                # design — every one would otherwise age in here within a day.
                stale = {
                    row.pubkey
                    for row in await select_stale_syncs_on_db(
                        db,
                        older_than=NOW - timedelta(hours=24),
                        limit=500,
                        now=NOW,
                        abandoned=ABANDONED_RULE,
                    )
                }
                assert ABANDONED not in stale, "abandoned checkout leaked into stale syncs"
                assert OTHER_ERROR in stale

                counted = {
                    row.pubkey
                    for row in await select_abandoned_checkouts_on_db(
                        db, limit=500, now=NOW, abandoned=ABANDONED_RULE
                    )
                }
                assert counted == {ABANDONED}, "the abandoned section disagrees with the sweep"

                await _cleanup(db)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_a_settled_subscription_stops_being_read_and_the_unsettled_stay():
    """Churn is unbounded and expired is terminal, so re-reading it is pure cost.

    Every probe here is stale enough to be picked up by the sweep's
    last-read clause, which is the one that would otherwise hold every dead row
    forever — so absence from the batch means the settled rule fired, not that
    the row failed to qualify in the first place.
    """
    async def _run():
        try:
            async with db_session() as db:
                await _cleanup(db)

                # Ran its course, holds nothing, Flash answered cleanly.
                await _seed(
                    db, SETTLED, status="expired", synced=READ_LAST_MONTH,
                )
                # Expired but still holding a policy: a tier standing on a dead
                # subscription is the anomaly the sweep exists to catch.
                await _seed(
                    db, SETTLED_GRANTED, status="expired", granted=4,
                    synced=READ_LAST_MONTH,
                )
                # Dropping this one would strand the error: a row we no longer
                # read can never clear it, so it would sit in failing_syncs for
                # good.
                await _seed(
                    db, SETTLED_ERRORED, status="expired", error="vault down",
                    since=LONG_AGO, synced=READ_LAST_MONTH,
                )
                # A pause is reversible and a cancellation is still on its way
                # to expiring. Asking Flash about either still does work.
                await _seed(db, PAUSED, status="paused", synced=READ_LAST_MONTH)
                await _seed(db, CANCELED, status="canceled", synced=READ_LAST_MONTH)
                await db.commit()

                selected = {
                    row.pubkey
                    for row in await select_reconcile_candidates_on_db(
                        db,
                        now=NOW,
                        stale_after=STALE_AFTER,
                        limit=500,
                        abandoned=ABANDONED_RULE,
                    )
                }

                assert SETTLED not in selected, "still re-reading a settled subscription"
                assert SETTLED_GRANTED in selected, "stopped watching a live tier"
                assert SETTLED_ERRORED in selected, "settled a row with an open error"
                assert PAUSED in selected, "a pause is reversible"
                assert CANCELED in selected, "a cancellation has not expired yet"

                # Same freeze as the abandoned checkouts: we stopped reading it,
                # so its read clock never moves again and it would age in here
                # within a day and never leave.
                stale = {
                    row.pubkey
                    for row in await select_stale_syncs_on_db(
                        db,
                        older_than=NOW - timedelta(hours=24),
                        limit=500,
                        now=NOW,
                        abandoned=ABANDONED_RULE,
                    )
                }
                assert SETTLED not in stale, "settled subscription leaked into stale syncs"
                assert PAUSED in stale, "a row we still read must stay visible"

                await _cleanup(db)
        finally:
            await engine.dispose()

    asyncio.run(_run())
