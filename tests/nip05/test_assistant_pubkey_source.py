"""Where the stored Assistant pubkey the well-known route matches against comes from.

`BrainstormNsec.pubkey` is the *owner* (the row's PK); the Assistant is the
public half of the row's nsec. Storing the owner would make every published
`nip05` unresolvable, so that distinction is pinned here.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from nostr_sdk import Keys

from app.repos.brainstorm_nsec import (
    backfill_assistant_identities_on_db,
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.utils.assistant_nip05 import compute_assistant_nip05_local_part


def _backfill(monkeypatch, rows: list[tuple[str, str, str | None]]) -> list[dict]:
    """Runs the backfill over ``rows``; returns the values each UPDATE wrote."""
    written: list[dict] = []

    async def _execute(_db, statement, _name):
        if statement.is_select:
            return SimpleNamespace(all=lambda: rows)
        written.append({c.key: v.value for c, v in statement._values.items()})

    monkeypatch.setattr("app.repos.brainstorm_nsec.execute_db_statement", _execute)
    asyncio.run(backfill_assistant_identities_on_db(AsyncMock()))
    return written


def test_backfill_stores_the_assistant_pubkey_not_the_owner(monkeypatch):
    keys = Keys.generate()
    owner = Keys.generate().public_key().to_hex()
    assistant = keys.public_key().to_hex()

    written = _backfill(monkeypatch, [(owner, keys.secret_key().to_bech32(), None)])

    assert written == [
        {
            "assistant_pubkey": assistant,
            "nip05_local_part": compute_assistant_nip05_local_part(assistant),
        }
    ]


def test_backfill_prefers_the_encrypted_nsec_over_the_plaintext_column(monkeypatch):
    encrypted_keys = Keys.generate()
    stale_plaintext = Keys.generate().secret_key().to_bech32()
    monkeypatch.setattr(
        "app.repos.brainstorm_nsec.decrypt_nsec",
        lambda _token: encrypted_keys.secret_key().to_bech32(),
    )

    written = _backfill(monkeypatch, [("owner", stale_plaintext, "a-fernet-token")])

    assert written[0]["assistant_pubkey"] == encrypted_keys.public_key().to_hex()


def test_one_unusable_row_does_not_sink_the_backfill(monkeypatch):
    good = Keys.generate()

    written = _backfill(
        monkeypatch,
        [("a", "not-an-nsec", None), ("b", good.secret_key().to_bech32(), None)],
    )

    assert [w["assistant_pubkey"] for w in written] == [good.public_key().to_hex()]


def test_a_new_row_is_born_with_its_assistant_identity(monkeypatch):
    monkeypatch.setattr(
        "app.repos.brainstorm_nsec.execute_db_statement",
        AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
    )
    db = MagicMock(flush=AsyncMock(), refresh=AsyncMock())

    row, created = asyncio.run(
        get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(db, "owner")
    )

    assistant = Keys.parse(secret_key=row.nsec).public_key().to_hex()
    assert created
    assert row.assistant_pubkey == assistant
    assert row.nip05_local_part == compute_assistant_nip05_local_part(assistant)
