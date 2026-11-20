"""Ticket: every NostrUser the server creates gets its user_id, by the id rule."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import settings
from app.message_queue_tasks import process_strfry_event as mod
from app.services import onboarding_service

ROOT = Path(__file__).resolve().parents[1]
AUTHOR = "a" * 64
T1, T2 = "b" * 64, "c" * 64
PROVIDER = "d" * 64


class _Session(MagicMock):
    """Doubles as its own transaction; records every write in call order."""

    def __init__(self, mismatched=(), **kw):
        super().__init__(**kw)
        self.log: list = []
        record = {"mismatched": list(mismatched), "removed": [], "targets": []}

        async def run(cypher, **params):
            self.log.append(("neo4j", cypher, params))
            result = MagicMock()
            result.single = AsyncMock(return_value=record)
            return result

        async def execute_write(work, *a, **k):
            return await work(self, *a, **k)

        self.run = AsyncMock(side_effect=run)
        self.execute_write = AsyncMock(side_effect=execute_write)


@pytest.fixture
def wired(monkeypatch):
    """ids on, table faked: each pubkey's id is 100 + its position in first-seen order."""
    calls: list = []
    table: dict[str, int] = {}
    session = _Session()

    async def ids_for(pubkeys, create=True):
        pubkeys = list(pubkeys)
        calls.append(pubkeys)
        session.log.append(("ids", pubkeys))
        for pk in pubkeys:
            table.setdefault(pk, 100 + len(table))
        return {pk: table[pk] for pk in pubkeys}

    monkeypatch.setattr(settings, "user_ids_enabled", True)
    monkeypatch.setattr(mod, "ids_for", ids_for)
    monkeypatch.setattr(mod, "_update_reverse_sets", AsyncMock())
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    monkeypatch.setattr(mod.redis_client, "pipeline", MagicMock(return_value=pipe))
    return session, calls, table


def _merges(session) -> list:
    return [e for e in session.log if e[0] == "neo4j" and "MERGE" in e[1]]


def _p(*pubkeys) -> list:
    return [["p", pk] for pk in pubkeys]


HANDLERS = {
    3: lambda s: mod.process_event_kind_3(s, {"pubkey": AUTHOR, "tags": _p(T1, T2)}),
    10000: lambda s: mod.process_event_kind_10000(
        s, {"pubkey": AUTHOR, "tags": _p(T1, T2)}
    ),
    1984: lambda s: mod.process_event_kind_1984(
        s, {"pubkey": AUTHOR, "tags": _p(T1, T2)}
    ),
    10040: lambda s: mod.process_event_kind_10040(
        s, {"pubkey": AUTHOR, "tags": [["30382:rank", T1], ["30382:rank", T2]]}
    ),
}


@pytest.mark.parametrize("kind", sorted(HANDLERS))
def test_each_handler_sets_ids_on_the_author_and_every_target(wired, kind):
    session, calls, table = wired
    asyncio.run(HANDLERS[kind](session))

    assert calls and set(calls[0]) == {AUTHOR, T1, T2}
    merges = _merges(session)
    assert len(merges) == 1
    _, cypher, params = merges[0]
    assert cypher.count("user_id IS NULL") == 2
    assert params["publisher_id"] == table[AUTHOR]
    assert params["targets"] == [
        {"pubkey": T1, "user_id": table[T1]},
        {"pubkey": T2, "user_id": table[T2]},
    ]


def test_the_ids_are_committed_before_the_graph_write_opens(wired):
    session, _, _ = wired
    asyncio.run(HANDLERS[3](session))
    kinds = [e[0] for e in session.log]
    assert kinds[0] == "ids"
    assert "ids" not in kinds[1:]


def test_a_failed_id_call_leaves_the_graph_untouched(wired, monkeypatch):
    session, _, _ = wired

    async def down(pubkeys, create=True):
        raise ConnectionError("scorecard database down")

    monkeypatch.setattr(mod, "ids_for", down)
    with pytest.raises(ConnectionError):
        asyncio.run(HANDLERS[3](session))
    assert session.log == []
    mod._update_reverse_sets.assert_not_awaited()


def test_a_node_holding_another_id_fails_the_event(wired, monkeypatch):
    _, calls, _ = wired
    session = _Session(mismatched=[T2])
    with pytest.raises(mod.UserIdMismatch) as err:
        asyncio.run(HANDLERS[3](session))
    assert T2 in str(err.value)
    # Raised inside the managed transaction, so the driver rolls it back.
    assert len(session.log) == 1
    mod._update_reverse_sets.assert_not_awaited()


def test_the_query_never_overwrites_an_id():
    cypher = mod._merge_with_ids_cypher("FOLLOWS")
    assert "SET" in cypher
    for line in cypher.splitlines():
        if "SET" in line:
            assert "user_id IS NULL" in line, line


def test_with_ids_off_nothing_changes(monkeypatch):
    monkeypatch.setattr(settings, "user_ids_enabled", False)
    monkeypatch.setattr(mod, "ids_for", AsyncMock(side_effect=AssertionError))
    monkeypatch.setattr(mod, "_update_reverse_sets", AsyncMock())
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    monkeypatch.setattr(mod.redis_client, "pipeline", MagicMock(return_value=pipe))
    for kind in HANDLERS:
        session = _Session()
        asyncio.run(HANDLERS[kind](session))
        for _, cypher, params in _merges(session):
            assert "user_id" not in cypher
            assert "publisher_id" not in params


def test_a_deletion_that_re_adds_report_edges_sets_ids(wired, monkeypatch):
    session, calls, table = wired
    monkeypatch.setattr(
        mod, "_current_report_targets", AsyncMock(return_value={"e" * 64})
    )
    monkeypatch.setattr(
        mod,
        "fetch_author_user_reports",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(mod, "surviving_report_targets", lambda r, a: {T1, T2})
    asyncio.run(mod.process_event_kind_5(session, {"pubkey": AUTHOR, "tags": []}))
    merges = _merges(session)
    assert len(merges) == 1
    assert {r["pubkey"] for r in merges[0][2]["targets"]} == {T1, T2}
    assert merges[0][2]["publisher_id"] == table[AUTHOR]


def test_the_onboarding_follow_list_takes_the_same_path(wired, monkeypatch):
    session, calls, _ = wired

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        onboarding_service.neo4j_driver, "session", MagicMock(return_value=_Ctx())
    )
    asyncio.run(
        onboarding_service._write_follows_with_retry(
            {"pubkey": AUTHOR, "kind": 3, "tags": _p(T1)}
        )
    )
    assert set(calls[0]) == {AUTHOR, T1}
    assert _merges(session)[0][2]["targets"][0]["pubkey"] == T1


# Any new place that creates a user node has to follow the id rule too.
_CREATES_A_USER = re.compile(r"MERGE\s*\(\s*\w*\s*:NostrUser")
_KNOWN = {
    "app/message_queue_tasks/process_strfry_event.py",
    "scripts/backfill_user_reports.py",
}


def test_no_other_code_creates_user_nodes():
    found = set()
    for folder in ("app", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            rel = str(path.relative_to(ROOT))
            if rel.startswith("scripts/test_"):
                continue
            if _CREATES_A_USER.search(path.read_text(encoding="utf-8")):
                found.add(rel)
    assert found == _KNOWN, (
        "a new MERGE of a NostrUser: give it user ids (see process_strfry_event."
        f"_write_with_ids) and list it here: {sorted(found - _KNOWN)}"
    )
