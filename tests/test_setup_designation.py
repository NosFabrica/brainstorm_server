"""kind-10040 designation rows served by GET /setup/{pubkey}.

The UI publishes these rows verbatim on the Observer's behalf, so the shape here
IS the wire. A wrong row is not a loud failure: a consumer that cannot parse a
row simply ignores it, and the symptom is a Trusted List nobody can discover.
Hence the assertions on exact shape rather than on "contains 30392 somewhere".
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from nostr_sdk import Keys

from app.api import app
from app.core.database import get_db
from app.routers.setup import router as setup_router

OBSERVER = "a" * 64


@pytest.fixture
def setup_client(client, monkeypatch):
    """The setup endpoint with its one DB read stubbed to a known assistant."""
    assistant = Keys.generate()
    row = MagicMock()
    row.nsec = assistant.secret_key().to_bech32()
    monkeypatch.setattr(
        setup_router,
        "select_brainstorm_nsec_by_pubkey_on_db",
        AsyncMock(return_value=row),
    )

    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[get_db] = _fake_db
    yield client, assistant.public_key().to_hex()
    app.dependency_overrides.pop(get_db, None)


def _rows(client):
    resp = client.get(f"/setup/{OBSERVER}")
    assert resp.status_code == 200
    return resp.json()


def test_trusted_lists_are_designated(setup_client):
    client, assistant_pubkey = setup_client
    rows = _rows(client)
    tl_rows = [r for r in rows if r[0].startswith("30392")]
    assert len(tl_rows) == 1, "exactly one generic entry per TL kind"
    assert tl_rows[0][1] == assistant_pubkey


def test_the_trusted_list_row_is_a_bare_kind_not_metric_parameterised(setup_client):
    """The row must be "30392" — no colon, no metric.

    Tapestry's deployed reader classifies `<kind>:<name>` on a 3039x as a
    reserved named-per-list override and treats it as unrecognized. So
    "30392:tag-membership" would be published happily and then ignored by every
    consumer, with no error anywhere. This test is the only thing standing
    between us and that silent failure.
    """
    client, _ = setup_client
    tl_row = next(r for r in _rows(client) if r[0].startswith("30392"))
    assert tl_row[0] == "30392"
    assert ":" not in tl_row[0]


def test_trusted_assertion_rows_are_still_metric_parameterised(setup_client):
    """The 3038x convention is the opposite one, and must not drift with it."""
    client, assistant_pubkey = setup_client
    ta_rows = [r for r in _rows(client) if r[0].startswith("30382")]
    assert {r[0] for r in ta_rows} == {
        "30382:rank",
        "30382:followers",
        "30382:reporters",
        "30382:muters",
        "30382:hops",
    }
    assert all(r[1] == assistant_pubkey for r in ta_rows)


def test_one_assistant_identity_signs_both_kinds(setup_client):
    """A consumer resolving either kind must land on the same provider."""
    client, _ = setup_client
    rows = _rows(client)
    assert len({r[1] for r in rows}) == 1


def test_every_row_is_a_well_formed_triple(setup_client):
    """A row whose second element is not 64-hex is not a delegation at all —
    readers classify it as 'other' regardless of kind."""
    client, _ = setup_client
    for row in _rows(client):
        assert len(row) == 3
        assert len(row[1]) == 64
        int(row[1], 16)  # hex, or this raises


def test_trusted_list_row_follows_the_configured_tl_relay(setup_client, monkeypatch):
    """The row must advertise where TLs are actually published, which is not
    necessarily where TAs are."""
    from app.core.config import settings

    client, _ = setup_client
    monkeypatch.setattr(settings, "trusted_list_relay", "wss://lists.example")
    tl_row = next(r for r in _rows(client) if r[0] == "30392")
    assert tl_row[2] == "wss://lists.example"


def test_trusted_list_row_falls_back_to_the_ta_relay(setup_client, monkeypatch):
    """Unset is the common case, and mirrors the service's own fallback."""
    from app.core.config import settings

    client, _ = setup_client
    monkeypatch.setattr(settings, "trusted_list_relay", "")
    rows = _rows(client)
    tl_row = next(r for r in rows if r[0] == "30392")
    ta_row = next(r for r in rows if r[0] == "30382:rank")
    assert tl_row[2] == ta_row[2]
