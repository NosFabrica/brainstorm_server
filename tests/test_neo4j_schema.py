"""The graph's schema is declared in one place and ensured from there."""
import asyncio
import re
from pathlib import Path

from app.neo4j_db import schema

ROOT = Path(__file__).resolve().parent.parent


class _Session:
    def __init__(self):
        self.ran: list[str] = []

    async def run(self, cypher, **params):
        self.ran.append(cypher)


def _ensure(**kwargs) -> tuple[list[str], list[str]]:
    session = _Session()
    return asyncio.run(schema.ensure_schema(session, **kwargs)), session.ran


def test_without_user_ids_only_the_pubkey_constraint_is_ensured(monkeypatch):
    monkeypatch.setattr(schema.settings, "user_ids_enabled", False)
    names, ran = _ensure()
    assert names == ["nostr_user_pubkey"]
    assert len(ran) == 1 and "u.pubkey IS UNIQUE" in ran[0]


def test_with_user_ids_the_id_constraint_is_ensured_too(monkeypatch):
    monkeypatch.setattr(schema.settings, "user_ids_enabled", True)
    names, ran = _ensure()
    assert names == [
        "nostr_user_pubkey",
        "nostr_user_user_id",
        "nostr_user_nip05_status",
    ]
    assert "u.user_id IS UNIQUE" in ran[1]
    assert "u.nip05_status" in ran[2]


def test_a_caller_about_to_write_ids_asks_for_them_whatever_the_setting(monkeypatch):
    monkeypatch.setattr(schema.settings, "user_ids_enabled", False)
    assert "nostr_user_user_id" in _ensure(user_ids=True)[0]


def test_every_statement_is_safe_to_run_again_and_named_as_listed():
    for item in schema.SCHEMA:
        assert "IF NOT EXISTS" in item.cypher, item.name
        assert f" {item.name} " in item.cypher, item.name
    assert len({item.name for item in schema.SCHEMA}) == len(schema.SCHEMA)


_CREATES_SCHEMA = re.compile(r"CREATE\s+(CONSTRAINT|(RANGE\s+|TEXT\s+)?INDEX)", re.I)


def test_schema_is_created_nowhere_else():
    """A constraint or index added in passing elsewhere would be missing after a
    rebuild; it belongs in `SCHEMA`."""
    offenders = [
        str(path.relative_to(ROOT))
        for folder in ("app", "scripts")
        for path in (ROOT / folder).rglob("*.py")
        if path != ROOT / "app" / "neo4j_db" / "schema.py"
        and _CREATES_SCHEMA.search(path.read_text())
    ]
    assert offenders == []
