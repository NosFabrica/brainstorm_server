"""POST /user/trustSignals rejects oversize and malformed batches before any
graph read. Parity with /overview lives in the integration suite."""

from unittest.mock import AsyncMock

import pytest

from app.api import app
from app.routers.user.dependencies import get_verified_cutoffs
from app.services.verified_cutoffs import FALLBACK_VERIFIED_CUTOFFS
from app.utils.api_validators import verify_token_optional

_HEX = "a" * 64


@pytest.fixture
def signals(client, monkeypatch) -> AsyncMock:
    app.dependency_overrides[verify_token_optional] = lambda: None
    app.dependency_overrides[get_verified_cutoffs] = lambda: FALLBACK_VERIFIED_CUTOFFS
    read = AsyncMock(return_value=[])
    monkeypatch.setattr("app.routers.user.router.get_trust_signals", read)
    return read


def test_more_than_500_pubkeys_is_413_even_when_malformed(client, signals):
    resp = client.post("/user/trustSignals", json={"pubkeys": ["nope"] * 501})
    assert resp.status_code == 413
    signals.assert_not_awaited()


def test_500_pubkeys_is_accepted(client, signals):
    resp = client.post("/user/trustSignals", json={"pubkeys": [_HEX] * 500})
    assert resp.status_code == 200


@pytest.mark.parametrize("bad", ["g" * 64, "a" * 63, "npub1xyz"])
def test_malformed_pubkey_is_422(client, signals, bad):
    resp = client.post("/user/trustSignals", json={"pubkeys": [_HEX, bad]})
    assert resp.status_code == 422
    signals.assert_not_awaited()


def test_empty_batch_is_422(client, signals):
    resp = client.post("/user/trustSignals", json={"pubkeys": []})
    assert resp.status_code == 422
