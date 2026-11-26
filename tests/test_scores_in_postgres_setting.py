"""SCORES_IN_POSTGRES: needs ids, and picks exactly one store for a run's writes."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.message_queue_tasks.write_neo4j_results as writer_module
from app.core.config import Settings


def _settings(**overrides) -> Settings:
    base = {
        name: getattr(writer_module.settings, name)
        for name in Settings.model_fields
        if name not in ("scorecard_db_url", "user_ids_enabled", "scores_in_postgres")
    }
    return Settings(**{**base, **overrides})


def test_scores_in_postgres_needs_user_ids():
    with pytest.raises(ValueError, match="SCORES_IN_POSTGRES needs USER_IDS_ENABLED"):
        _settings(scorecard_db_url="", user_ids_enabled=False, scores_in_postgres=True)
    with pytest.raises(ValueError, match="SCORES_IN_POSTGRES needs USER_IDS_ENABLED"):
        _settings(
            scorecard_db_url="postgresql+asyncpg://x/y",
            user_ids_enabled=False,
            scores_in_postgres=True,
        )
    on = _settings(
        scorecard_db_url="postgresql+asyncpg://x/y",
        user_ids_enabled=True,
        scores_in_postgres=True,
    )
    assert on.scores_in_postgres


def test_it_is_off_by_default():
    assert Settings.model_fields["scores_in_postgres"].default is False


@pytest.fixture
def stores(monkeypatch):
    @asynccontextmanager
    async def _fake_db_session():
        yield AsyncMock()

    monkeypatch.setattr(writer_module, "db_session", _fake_db_session)
    for name, value in {
        "update_brainstorm_request_internal_publication_status_by_id_on_db": None,
        "select_brainstorm_request_by_id_on_db": SimpleNamespace(force_full_neo4j=True),
        "select_compromised_pubkeys_on_db": set(),
        "get_last_neo4j_cleanup_at_on_db": None,
        "mark_neo4j_cleanup_on_db": None,
    }.items():
        monkeypatch.setattr(writer_module, name, AsyncMock(return_value=value))
    postgres = AsyncMock(return_value=0)
    neo4j = AsyncMock()
    unreached = AsyncMock(return_value=0)
    monkeypatch.setattr(writer_module, "_write_to_postgres", postgres)
    monkeypatch.setattr(writer_module, "_write_batches", neo4j)
    monkeypatch.setattr(writer_module, "_clear_unreached", unreached)
    return SimpleNamespace(postgres=postgres, neo4j=neo4j, unreached=unreached)


def _message() -> dict:
    card = {
        "observer": "o" * 64,
        "observee": "a" * 64,
        "influence": 0.5,
        "hops": 1,
        "trusted_followers": 2,
    }
    return {
        "private_id": 1,
        "result": {"success": True, "scorecards": {"a" * 64: card}},
    }


def test_off_writes_neo4j_only(stores, monkeypatch):
    monkeypatch.setattr(writer_module.settings, "scores_in_postgres", False)
    asyncio.run(writer_module.process_neo4j_write_message(_message()))
    assert stores.neo4j.await_count == 2
    stores.unreached.assert_awaited_once()
    stores.postgres.assert_not_awaited()


def test_on_writes_postgres_only(stores, monkeypatch):
    monkeypatch.setattr(writer_module.settings, "scores_in_postgres", True)
    asyncio.run(writer_module.process_neo4j_write_message(_message()))
    stores.postgres.assert_awaited_once()
    observer, cards, clear_set, reached = stores.postgres.await_args.args
    assert observer == "o" * 64
    assert [c["observee"] for c in cards] == ["a" * 64]
    assert reached == {"a" * 64}
    stores.neo4j.assert_not_awaited()
    stores.unreached.assert_not_awaited()
