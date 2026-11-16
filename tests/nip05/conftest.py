"""Fixtures for the NIP-05 well-known E2E tests.

Boots a real-but-minimal FastAPI app carrying the production `nip05` router. The
DB is the only boundary stubbed: `get_db` yields a mock session and the
local-part lookup is patched at its import site.

Settings env is bootstrapped in the parent `tests/conftest.py`.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import get_db
from app.routers.nip05.router import router as nip05_router
from app.utils.assistant_nip05 import compute_assistant_nip05_local_part


@pytest.fixture()
def assistant_pubkeys_query() -> AsyncMock:
    """Stands in for `select_assistant_pubkey_by_nip05_local_part_on_db`."""
    return AsyncMock(return_value=None)


@pytest.fixture()
def make_client(monkeypatch, assistant_pubkeys_query: AsyncMock):
    def _make(
        *,
        assistants: tuple[str, ...] = (),
        house_pubkey: str = "",
    ) -> TestClient:
        monkeypatch.setattr(settings, "periodic_graperank_pubkey", house_pubkey)
        by_local_part = {compute_assistant_nip05_local_part(pk): pk for pk in assistants}
        assistant_pubkeys_query.side_effect = lambda _db, name: by_local_part.get(name)
        monkeypatch.setattr(
            "app.services.nip05_service.select_assistant_pubkey_by_nip05_local_part_on_db",
            assistant_pubkeys_query,
        )

        app = FastAPI()
        # Mirror the production CORS posture so the explicit header on the
        # response is tested against the middleware, not in isolation.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        app.include_router(nip05_router)

        async def _fake_get_db():
            yield MagicMock()

        app.dependency_overrides[get_db] = _fake_get_db
        return TestClient(app)

    return _make
