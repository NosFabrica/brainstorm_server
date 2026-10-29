"""`assistant_publish.sign_checked`: what the Duty Menu and the Treasure Map sign
is checked before anything is sent, and is a 500 otherwise. Each of its three
checks has a case of its own, so none can go without a test failing.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from nostr_sdk import EventBuilder, Keys

from app.services import assistant_publish
from app.services.assistant_publish import sign_checked
from app.utils.client_tag import CLIENT_TAG

RELAY = "wss://scores.example"


def _sign(keys: Keys, tags: list[list[str]], check=lambda signed: True, **kwargs):
    return sign_checked(
        keys, 10040, "", tags, int(time.time()), check, "Treasure Map", **kwargs
    )


def _refused(**kwargs) -> int:
    with pytest.raises(HTTPException) as refused:
        _sign(**kwargs)
    assert isinstance(refused.value.detail, str)
    return refused.value.status_code


def test_an_event_that_passes_is_signed_with_exactly_its_tags():
    keys = Keys.generate()
    tags = [["30382:rank", keys.public_key().to_hex(), RELAY]]

    event, signed = _sign(keys, tags)

    assert event.verify()
    assert signed["tags"] == [*tags, CLIENT_TAG]


def test_an_event_that_fails_the_callers_check_is_a_500():
    assert _refused(keys=Keys.generate(), tags=[], check=lambda signed: False) == 500


def test_a_tag_the_signer_drops_is_a_500():
    """nostr-sdk drops a `p` naming the signer unless self-tagging is allowed,
    as the Duty Menu's `p` (§ 4) needs."""
    keys = Keys.generate()
    tags = [["p", keys.public_key().to_hex()]]

    assert _refused(keys=keys, tags=tags) == 500
    _, signed = _sign(keys, tags, self_tagging=True)
    assert signed["tags"][0] == tags[0]


def test_an_event_that_does_not_verify_is_a_500(monkeypatch):
    class Unverified:
        """nostr-sdk's EventBuilder, but what it signs doesn't verify."""

        def __init__(self, **kwargs):
            self.real = EventBuilder(**kwargs)

        def tags(self, tags):
            self.real = self.real.tags(tags)
            return self

        def custom_created_at(self, created_at):
            self.real = self.real.custom_created_at(created_at)
            return self

        def sign_with_keys(self, keys):
            event = self.real.sign_with_keys(keys)
            return SimpleNamespace(verify=lambda: False, as_json=event.as_json)

    monkeypatch.setattr(assistant_publish, "EventBuilder", Unverified)

    assert _refused(keys=Keys.generate(), tags=[]) == 500
