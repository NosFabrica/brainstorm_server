"""Admin-initiated GrapeRank runs are tagged Admin, not Manual (lane + quota)."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nostr_sdk import Keys

from app.core.database import get_db
from app.db_models import TriggerSource
from app.routers.admin.router import verify_admin_access

PUBKEY = "a" * 64


def _nsec_row():
    return SimpleNamespace(
        pubkey=PUBKEY,
        nsec=Keys.generate().secret_key().to_hex(),
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


def test_admin_trigger_graperank_is_tagged_admin(admin_client, monkeypatch):
    create = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.routers.brainstorm_pubkey.router.select_brainstorm_nsec_by_pubkey_on_db",
        AsyncMock(return_value=_nsec_row()),
    )
    monkeypatch.setattr(
        "app.routers.brainstorm_pubkey.router.create_brainstorm_request", create
    )

    response = admin_client.post(f"/admin/brainstormPubkey/{PUBKEY}/trigger_graperank")

    assert response.status_code == 200
    assert create.await_args.kwargs["trigger_source"] == TriggerSource.ADMIN.value


def test_admin_observer_creation_auto_trigger_is_tagged_admin(
    admin_client, monkeypatch
):
    create = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.brainstorm_pubkey_service."
        "get_or_create_brainstorm_observer_nsec_by_pubkey_on_db",
        AsyncMock(return_value=(_nsec_row(), True)),
    )
    monkeypatch.setattr(
        "app.services.brainstorm_pubkey_service.create_brainstorm_request", create
    )

    response = admin_client.get(f"/admin/brainstormPubkey/{PUBKEY}")

    assert response.status_code == 200
    assert create.await_args.kwargs["trigger_source"] == TriggerSource.ADMIN.value
