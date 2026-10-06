"""The ORE-05 lens gate compares the relay's lens service against the pov's
Assistant pubkey (public half of the row's nsec), never the owner pubkey."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nostr_sdk import Keys

from app.repos.brainstorm_nsec import PovSearchFields, get_pov_search_fields_on_db


def _fields_for(monkeypatch, row):
    monkeypatch.setattr(
        "app.repos.brainstorm_nsec.execute_db_statement",
        AsyncMock(return_value=SimpleNamespace(first=lambda: row)),
    )
    return asyncio.run(get_pov_search_fields_on_db(AsyncMock(), "a" * 64))


def test_returns_assistant_pubkey_and_publish_time(monkeypatch):
    keys = Keys.generate()
    published = datetime(2026, 10, 1, 12, 0)

    result = _fields_for(monkeypatch, (keys.secret_key().to_bech32(), None, published))

    assert result == PovSearchFields(keys.public_key().to_hex(), published)


def test_missing_row_has_no_assistant(monkeypatch):
    assert _fields_for(monkeypatch, None) == PovSearchFields(None, None)


def test_unusable_nsec_is_flagged_not_mistaken_for_a_missing_row(monkeypatch):
    published = datetime(2026, 10, 1, 12, 0)

    assert _fields_for(monkeypatch, ("not-an-nsec", None, published)) == (
        PovSearchFields(None, published, nsec_unusable=True)
    )
