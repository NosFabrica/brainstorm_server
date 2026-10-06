"""Admin resync re-asserts one observer's full state on the relay."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.core.database import get_db
from app.db_models import TriggerSource
from app.routers.admin.router import verify_admin_access
from app.schemas.schemas import BrainstormRequestInstance

PUBKEY = "a" * 64


def _request() -> BrainstormRequestInstance:
    return BrainstormRequestInstance(
        private_id=1,
        status="waiting",
        ta_status=None,
        internal_publication_status=None,
        count_values=None,
        password="x",
        algorithm="graperank",
        parameters=PUBKEY,
        how_many_others_with_priority=0,
        pubkey=PUBKEY,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )


@pytest.fixture
def admin_client(client):
    async def _fake_get_db():
        yield AsyncMock()

    from app.api import app

    app.dependency_overrides[verify_admin_access] = lambda: None
    app.dependency_overrides[get_db] = _fake_get_db
    yield client


@pytest.mark.parametrize("params", [{}, {"target": "both"}, {"target": "vespa"}])
def test_resync_forces_a_full_relay_run(admin_client, monkeypatch, params):
    # A stale caller still sending a Vespa target gets the relay resync.
    create = AsyncMock(return_value=_request())
    monkeypatch.setattr(
        "app.routers.admin.users.router.create_brainstorm_request", create
    )

    response = admin_client.post(f"/admin/users/{PUBKEY}/resync", params=params)

    assert response.status_code == 200
    assert create.await_args.kwargs == {
        "db": create.await_args.kwargs["db"],
        "algorithm": "graperank",
        "parameters": PUBKEY,
        "pubkey": PUBKEY,
        "force_full_relay": True,
        "trigger_source": TriggerSource.ADMIN.value,
    }
