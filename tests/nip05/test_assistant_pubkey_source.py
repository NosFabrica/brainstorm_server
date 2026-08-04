"""Where the Assistant pubkey the well-known route matches against comes from.

`BrainstormNsec.pubkey` is the *owner* (the row's PK); the Assistant is the
public half of the row's nsec. Returning the column would make every published
`nip05` unresolvable, so that distinction is pinned here.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nostr_sdk import Keys

from app.repos.brainstorm_nsec import select_all_assistant_pubkeys_on_db


def _pubkeys_for(monkeypatch, rows: list[tuple[str, str | None]]) -> list[str]:
    monkeypatch.setattr(
        "app.repos.brainstorm_nsec.execute_db_statement",
        AsyncMock(return_value=SimpleNamespace(all=lambda: rows)),
    )
    return asyncio.run(select_all_assistant_pubkeys_on_db(AsyncMock()))


def test_returns_the_assistant_pubkey_not_the_owner(monkeypatch):
    keys = Keys.generate()
    owner_pubkey = Keys.generate().public_key().to_hex()

    result = _pubkeys_for(monkeypatch, [(keys.secret_key().to_bech32(), None)])

    assert result == [keys.public_key().to_hex()]
    assert owner_pubkey not in result


def test_prefers_the_encrypted_nsec_over_the_plaintext_column(monkeypatch):
    encrypted_keys = Keys.generate()
    stale_plaintext = Keys.generate().secret_key().to_bech32()
    monkeypatch.setattr(
        "app.repos.brainstorm_nsec.decrypt_nsec",
        lambda _token: encrypted_keys.secret_key().to_bech32(),
    )

    result = _pubkeys_for(monkeypatch, [(stale_plaintext, "a-fernet-token")])

    assert result == [encrypted_keys.public_key().to_hex()]


def test_one_unusable_row_does_not_sink_the_whole_lookup(monkeypatch):
    good = Keys.generate()

    result = _pubkeys_for(
        monkeypatch, [("not-an-nsec", None), (good.secret_key().to_bech32(), None)]
    )

    assert result == [good.public_key().to_hex()]
