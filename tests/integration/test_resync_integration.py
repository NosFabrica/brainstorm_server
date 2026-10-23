"""Integration test for the admin resync endpoint (published-state-drift repair).

Requires the real local Postgres (e.g. ``docker compose up -d``). Run with::

    poetry run pytest tests/integration -m integration

Drives ``POST /admin/users/{pubkey}/resync`` against the live DB and asserts the
enqueued BrainstormRequest row carries ``force_full_relay`` for exactly that one
observer. The Redis enqueue is stubbed so the test doesn't
push a real GrapeRank job onto the shared queue.

The request and the read-back run on a single asyncio loop (via httpx's
ASGITransport, not TestClient's separate portal loop) so the shared async DB
engine's asyncpg connections stay bound to one loop; the engine is disposed
inside that loop to avoid "event loop is closed" teardown races.
"""

import asyncio
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
            nostr_pubkey=observer
        )

    async def _fake_admin() -> None:
        return None

    # Don't push a real GrapeRank job onto the shared queue from a test. The
    # enqueue lives in scheduler_lanes.enqueue_calc_request -> redis_client.rpush.
    monkeypatch.setattr("app.services.scheduler_lanes.redis_client.rpush", AsyncMock())
    app.dependency_overrides[verify_token] = _fake_verify_token
    app.dependency_overrides[verify_admin_access] = _fake_admin
    try:
        yield observer
    finally:
        app.dependency_overrides.clear()


async def _post_resync(observer: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/admin/users/{observer}/resync")


def test_resync_persists_force_full_relay_for_one_observer(admin_observer):
    async def _run():
        try:
            resp = await _post_resync(admin_observer)
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
                # A single recompute for this one observer, never a fan-out.
                assert row.force_full_relay is True
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
