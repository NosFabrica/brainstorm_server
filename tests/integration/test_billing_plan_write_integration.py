"""Plan-mapping writes, executed against a real Postgres.

The bug this covers reached staging and returned a 500 on every plan edit that
actually changed something — while the write itself landed, so an admin saw an
error, retried, got a 200 from a no-op, and believed nothing had happened.

`updated_at` is `onupdate=func.now()`, so an UPDATE leaves it server-generated
and expired even though the session is `expire_on_commit=False`. Building the
response then lazy-loads it from inside FastAPI's async path, which raises
`MissingGreenlet`. Reading any timestamp off the returned object is what fails,
so that is exactly what these assert.

Invisible to `tests/test_billing_admin.py`: it stubs the session, where
attributes are plain values with no expiry semantics. Only a real DB has them.

Requires the local Postgres (e.g. ``docker compose up -d``). Run with::

    poetry run pytest tests/integration -m integration
"""

import asyncio

import pytest
from sqlalchemy import text

from app.core.database import db_session
from app.services.billing_service import create_billing_plan, update_billing_plan

pytestmark = pytest.mark.integration

SERVICE_ID = "svc-write-probe"


async def _policy_id(db) -> int:
    row = (
        await db.execute(text("SELECT id FROM scheduling ORDER BY id LIMIT 1"))
    ).first()
    assert row is not None, "no scheduling policy to map a plan onto"
    return row[0]


async def _cleanup(db) -> None:
    await db.execute(
        text("DELETE FROM billing_plan WHERE flash_service_id = :s"), {"s": SERVICE_ID}
    )
    await db.commit()


def test_a_plan_write_returns_an_object_whose_timestamps_can_be_read():
    async def _run():
        async with db_session() as db:
            await _cleanup(db)
            try:
                scheduling_id = await _policy_id(db)

                created = await create_billing_plan(
                    db,
                    {
                        "flash_service_id": SERVICE_ID,
                        "flash_plan_id": "plan-write-probe",
                        "scheduling_id": scheduling_id,
                        "amount_minor": 200,
                        "currency": "USD",
                        "is_active": True,
                    },
                )
                # The assertion IS the attribute access: unrefreshed, this
                # raises MissingGreenlet rather than returning a value.
                assert created.updated_at is not None
                assert created.created_at is not None

                # An edit that genuinely changes something — the no-op case
                # never reproduced the bug, which is why it looked flaky.
                edited = await update_billing_plan(
                    db, created.id, {"includes": ["one", "two"]}
                )
                assert edited.updated_at is not None
                assert edited.includes == ["one", "two"]
            finally:
                await _cleanup(db)

    asyncio.run(_run())
