"""Fixtures for the Open Ranking E2E tests.

Boots a real-but-minimal FastAPI app that mounts ONLY the Open Ranking router
and exercises it through `TestClient` (real ASGI dispatch, real Pydantic
validation, real exception handlers, real CORS). Keeping the app minimal makes
these tests independent of the full brainstorm_server lifespan / background
tasks. External data sources (Neo4j, Redis, Vespa) are stubbed per-test.

Settings env and the NWT helpers (`make_nwt`) are bootstrapped in the parent
`tests/conftest.py`.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from tests.conftest import make_nwt


# ---------------------------------------------------------------------------
# App factory: only mounts the Open Ranking router.
# ---------------------------------------------------------------------------
def _build_app() -> FastAPI:
    from app.core.database import get_db
    from app.routers.open_ranking.errors import install_ore_error_handlers
    from app.routers.open_ranking.router import router as open_ranking_router

    app = FastAPI()
    # Mirror the CORS posture of the production app so OPTIONS preflight tests
    # behave the same here.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Reason", "Retry-After"],
    )
    # Same error shape as the production app ({"error": ...} + X-Reason).
    install_ore_error_handlers(app)
    app.include_router(open_ranking_router)

    # Postgres is not part of this suite. The availability gate's repo reads
    # are patched per-test; the session dependency yields a sentinel so any
    # unpatched code path that actually touches the session fails loudly
    # instead of dialing a real database.
    async def _no_db():
        yield None

    app.dependency_overrides[get_db] = _no_db
    return app


@pytest.fixture()
def app() -> FastAPI:
    return _build_app()


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    """A fresh, valid NWT for every test that needs an authenticated call. The
    signing key is regenerated per-test so token reuse across cases can't
    accidentally pass.
    """
    token, _pk = make_nwt()
    return {"Authorization": f"Nostr {token}"}
