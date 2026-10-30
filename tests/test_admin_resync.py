"""Admin resync re-asserts one observer's full state on the relay and/or Neo4j."""

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


@pytest.mark.parametrize(
    "params, relay, neo4j",
    [
        ({}, True, True),
        ({"target": "both"}, True, True),
        ({"target": "relay"}, True, False),
        ({"target": "neo4j"}, False, True),
    ],
)
def test_resync_forces_a_full_run_per_target(
    admin_client, monkeypatch, params, relay, neo4j
):
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
        "force_full_relay": relay,
        "force_full_neo4j": neo4j,
        "trigger_source": TriggerSource.ADMIN.value,
    }


def test_resync_rejects_an_unknown_target(admin_client, monkeypatch):
    create = AsyncMock(return_value=_request())
    monkeypatch.setattr(
        "app.routers.admin.users.router.create_brainstorm_request", create
    )

    response = admin_client.post(
        f"/admin/users/{PUBKEY}/resync", params={"target": "vespa"}
    )

    assert response.status_code == 422
    create.assert_not_awaited()
