"""Payload pruning, executed against a real Postgres.

Requires the local Postgres (e.g. ``docker compose up -d``). Run with::

    poetry run pytest tests/integration -m integration

`tests/test_billing_repo.py` builds every billing statement but stubs the
execute, so it proves the queries name real *model attributes* and nothing more.
Both bugs this file covers were invisible there and shipped to staging:

1. `jsonb_set`'s path argument is `text[]`. A bound Python string arrives as
   `varchar`, which Postgres will not coerce, so the statement raised
   `UndefinedFunctionError` and the prune never ran at all.
2. `jsonb_set` is STRICT and `to_jsonb(NULL::varchar)` is SQL NULL, so the
   original would have nulled the WHOLE payload — destroying the amounts the
   accounting export reads. Fixing (1) alone would have turned a loud crash into
   silent data loss.

Neither is reachable without a database: SQLAlchemy compiles both happily.
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.database import db_session, engine
from app.repos.flash_webhook_event_repo import (
    PERSONAL_PAYLOAD_FIELDS,
    prune_webhook_payloads_on_db,
)

pytestmark = pytest.mark.integration

DEDUPE_KEY = "prune-integration-probe"
OLD = datetime.now() - timedelta(days=200)
CUTOFF = datetime.now() - timedelta(days=90)

# Personal fields must go; everything else is accounting and outlives them.
ACCOUNTING = {
    "subscriptionId": "sub_probe",
    "externalRef": "f" * 64,
    "amount": 200,
    "currency": "USD",
    "invoiceId": "inv_probe",
}
PERSONAL = {"email": "jane@example.com", "name": "Jane Doe"}


async def _seed(db) -> None:
    await db.execute(
        text("DELETE FROM flash_webhook_event WHERE dedupe_key = :k"),
        {"k": DEDUPE_KEY},
    )
    await db.execute(
        text(
            """
            INSERT INTO flash_webhook_event
              (event, delivery_timestamp, subscription_id, payload, dedupe_key,
               processed_at, attempts, created_at, updated_at)
            VALUES ('subscription.renewed', 1, 'sub_probe', CAST(:p AS jsonb),
                    :k, :old, 1, :old, :old)
            """
        ),
        {
            "p": json.dumps(
                {"event": "subscription.renewed", "data": {**ACCOUNTING, **PERSONAL}}
            ),
            "k": DEDUPE_KEY,
            "old": OLD,
        },
    )
    await db.commit()


async def _payload(db) -> dict | None:
    raw = (
        await db.execute(
            text("SELECT payload::text FROM flash_webhook_event WHERE dedupe_key = :k"),
            {"k": DEDUPE_KEY},
        )
    ).scalar()
    return None if raw is None else json.loads(raw)


async def _cleanup(db) -> None:
    await db.execute(
        text("DELETE FROM flash_webhook_event WHERE dedupe_key = :k"), {"k": DEDUPE_KEY}
    )
    await db.commit()


def test_pruning_redacts_personal_data_and_keeps_the_accounting_record():
    async def _run():
        try:
            async with db_session() as db:
                await _seed(db)

                count = await prune_webhook_payloads_on_db(db, older_than=CUTOFF)
                await db.commit()
                assert count == 1

                payload = await _payload(db)
                # The whole payload surviving is the point: nulling it would
                # take the amounts the CSV export reads.
                assert payload is not None, "prune nulled the entire payload"
                data = payload["data"]

                for field in PERSONAL_PAYLOAD_FIELDS:
                    assert field not in data, f"{field} survived pruning"
                for field, value in ACCOUNTING.items():
                    assert data[field] == value, f"{field} was lost"

                await _cleanup(db)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_pruning_the_same_row_twice_changes_nothing():
    """A key set to null still satisfies the `?|` predicate, so a redaction that
    nulls rather than deletes rewrites the same rows every cycle, forever."""

    async def _run():
        try:
            async with db_session() as db:
                await _seed(db)

                assert await prune_webhook_payloads_on_db(db, older_than=CUTOFF) == 1
                await db.commit()
                assert await prune_webhook_payloads_on_db(db, older_than=CUTOFF) == 0
                await db.commit()

                await _cleanup(db)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_pruning_leaves_an_unprocessed_event_alone():
    """Replay reads the payload to find the subscriber, so redacting one that
    has not been applied yet would strand it."""

    async def _run():
        try:
            async with db_session() as db:
                await _seed(db)
                await db.execute(
                    text(
                        "UPDATE flash_webhook_event SET processed_at = NULL "
                        "WHERE dedupe_key = :k"
                    ),
                    {"k": DEDUPE_KEY},
                )
                await db.commit()

                assert await prune_webhook_payloads_on_db(db, older_than=CUTOFF) == 0
                await db.commit()

                data = (await _payload(db))["data"]
                assert data["email"] == PERSONAL["email"]

                await _cleanup(db)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_pruning_leaves_a_recent_event_alone():
    async def _run():
        try:
            async with db_session() as db:
                await _seed(db)

                count = await prune_webhook_payloads_on_db(
                    db, older_than=datetime.now() - timedelta(days=365)
                )
                await db.commit()
                assert count == 0

                data = (await _payload(db))["data"]
                assert data["email"] == PERSONAL["email"]

                await _cleanup(db)
        finally:
            await engine.dispose()

    asyncio.run(_run())
