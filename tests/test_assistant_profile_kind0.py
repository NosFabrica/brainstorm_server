"""What the Assistant's kind 0 actually carries when it goes out.

Drives `POST /user/assistantProfile` through the real router + service, with the
Nostr client faked at its import site: `sign_event_builder` signs the *real*
builder with the Assistant's keys, so `send_event` receives a real event whose
content JSON is the one clients will see.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nostr_sdk import EventBuilder, Keys, Kind

from app.core.config import settings
from app.core.database import get_db


@pytest.fixture
def assistant_keys() -> Keys:
    return Keys.generate()


@pytest.fixture
def sent_events(client, monkeypatch, assistant_keys) -> list:
    """Fake the whole Nostr side; return the list of events handed to send_event."""
    from app.api import app

    captured: list = []
    owner_kind0 = EventBuilder(Kind(0), json.dumps({"name": "alice"})).sign_with_keys(
        Keys.generate()
    )

    async def _fake_get_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _fake_get_db

    monkeypatch.setattr(
        "app.services.assistant_profile_service.select_brainstorm_nsec_by_pubkey_on_db",
        AsyncMock(
            return_value=SimpleNamespace(nsec=assistant_keys.secret_key().to_bech32())
        ),
    )

    class _FakeClient:
        def __init__(self, *_, **__):
            self.relays: list[str] = []

        async def add_relay(self, url):
            self.relays.append(url)

        async def connect(self):
            pass

        async def fetch_events(self, *_, **__):
            return SimpleNamespace(to_vec=lambda: [owner_kind0])

        async def sign_event_builder(self, builder):
            return builder.sign_with_keys(assistant_keys)

        async def send_event(self, event):
            captured.append(event)
            return SimpleNamespace(success=["ok"], failed={})

        async def disconnect(self):
            pass

    monkeypatch.setattr("app.services.assistant_profile_service.Client", _FakeClient)

    return captured


def _publish(client) -> dict:
    r = client.post("/user/assistantProfile")
    assert r.status_code == 200, r.text
    return r.json()


def _content(sent_events: list) -> dict:
    assert len(sent_events) == 1
    return json.loads(sent_events[0].content())


def test_website_is_the_frontend_url(client, sent_events):
    _publish(client)
    assert _content(sent_events)["website"] == settings.frontend_url


def test_nip05_is_the_derivation_of_the_assistant_pubkey(
    client, sent_events, assistant_keys
):
    from app.utils.assistant_nip05 import compute_assistant_nip05

    _publish(client)
    expected = compute_assistant_nip05(assistant_keys.public_key().to_hex())
    assert expected is not None
    assert _content(sent_events)["nip05"] == expected


def test_nip05_is_omitted_when_frontend_url_has_no_hostname(
    client, sent_events, monkeypatch
):
    monkeypatch.setattr(settings, "frontend_url", "")

    _publish(client)
    assert "nip05" not in _content(sent_events)


def test_existing_metadata_fields_are_preserved(client, sent_events):
    _publish(client)
    content = _content(sent_events)
    assert content["name"] == "alice's Brainstorm Assistant"
    assert content["display_name"] == "alice's Brainstorm Assistant"
    assert content["about"].startswith("I am the Brainstorm Assistant")


def test_response_carries_the_event_id_and_assistant_pubkey(
    client, sent_events, assistant_keys
):
    body = _publish(client)["data"]
    assert body["assistant_pubkey"] == assistant_keys.public_key().to_hex()
    assert body["event_id"] == sent_events[0].id().to_hex()


def test_owner_name_lookup_failure_degrades_to_the_pubkey_prefix(
    client, sent_events, monkeypatch, caller
):
    """A relay hiccup on the owner kind-0 read must not abort the whole
    publish — the upload task leans on this to never skip a profile."""
    monkeypatch.setattr(
        "app.services.assistant_profile_service._fetch_owner_name",
        AsyncMock(side_effect=Exception("relay timed out")),
    )

    _publish(client)

    expected = f"{caller.pubkey[:6]}'s Brainstorm Assistant"
    assert _content(sent_events)["name"] == expected
