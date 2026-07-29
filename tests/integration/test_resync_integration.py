"""Integration test for the admin resync endpoint (published-state-drift repair).

Requires the real local Postgres (e.g. ``docker compose up -d``). Run with::

    poetry run pytest tests/integration -m integration

Drives ``POST /admin/users/{pubkey}/resync`` against the live DB and asserts the
enqueued BrainstormRequest row carries the right per-sink ``force_full_*`` flags
for exactly that one observer. The Redis enqueue is stubbed so the test doesn't
push a real GrapeRank job onto the shared queue.

The request and the read-back run on a single asyncio loop (via httpx's
ASGITransport, not TestClient's separate portal loop) so the shared async DB
engine's asyncpg connections stay bound to one loop; the engine is disposed
inside that loop to avoid "event loop is closed" teardown races.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Request
from nostr_sdk import Keys
from sqlalchemy import delete, select

from app.api import app
from app.core.database import db_session, engine
from app.db_models import BrainstormNsec, BrainstormRequest
from app.routers.admin.router import verify_admin_access
from app.utils.api_validators import verify_token
from app.utils.auth.auth_models import JWTData

pytestmark = pytest.mark.integration


@pytest.fixture
def admin_observer(monkeypatch) -> str:
    observer = Keys.generate().public_key().to_hex()

    async def _fake_verify_token(request: Request) -> None:
        request.state.jwt_data = JWTData(
            nostr_pubkey=observer, expires_date=datetime.max
        )

    async def _fake_admin() -> None:
        return None

    # Don't push a real GrapeRank job onto the shared queue from a test. The
    # enqueue lives in scheduler_lanes.enqueue_calc_request -> redis_client.rpush.
    monkeypatch.setattr(
        "app.services.scheduler_lanes.redis_client.rpush", AsyncMock()
    )
    app.dependency_overrides[verify_token] = _fake_verify_token
    app.dependency_overrides[verify_admin_access] = _fake_admin
    try:
        yield observer
    finally:
        app.dependency_overrides.clear()


async def _post_resync(observer: str, target: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            f"/admin/users/{observer}/resync", params={"target": target}
        )


def test_resync_rejects_unknown_target(admin_observer):
    async def _run():
        try:
            resp = await _post_resync(admin_observer, "everything")
            assert resp.status_code == 422
        finally:
            await engine.dispose()

    asyncio.run(_run())


@pytest.mark.parametrize(
    "target,expected",
    [("relay", (True, False)), ("vespa", (False, True)), ("both", (True, True))],
)
def test_resync_persists_force_full_flags_for_one_observer(
    admin_observer, target, expected
):
    async def _run():
        try:
            resp = await _post_resync(admin_observer, target)
            assert resp.status_code == 200
            request_id = resp.json()["data"]["private_id"]

            async with db_session() as db:
                row = (
                    await db.execute(
                        select(BrainstormRequest).where(
                            BrainstormRequest.private_id == request_id
                        )
                    )
                ).scalar_one()
                # The row carries the right per-sink overrides, for this one
                # observer (a single recompute, never a fan-out).
                assert (
                    bool(row.force_full_relay),
                    bool(row.force_full_vespa),
                ) == expected
                assert row.pubkey == admin_observer

                await db.execute(
                    delete(BrainstormRequest).where(
                        BrainstormRequest.private_id == request_id
                    )
                )
                await db.execute(
                    delete(BrainstormNsec).where(
                        BrainstormNsec.pubkey == admin_observer
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())
