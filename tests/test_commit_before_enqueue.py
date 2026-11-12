"""A new request row is committed before its job is queued: an idle worker can
finish and post results before the caller commits, and the row must exist then."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db_models import BrainstormRequestStatus
from app.services import brainstorm_request_service as service


def _create(monkeypatch, enqueue) -> tuple[list, MagicMock]:
    events: list = []
    db = MagicMock()
    db.commit = AsyncMock(side_effect=lambda: events.append("commit"))

    async def _enqueue(*args, **kwargs):
        events.append("enqueue")
        enqueue()

    async def _set_status(db, request_id, status):
        events.append((request_id, status))

    for name, value in {
        "fetch_designated_pubkeys": AsyncMock(return_value=[]),
        "get_graperank_preset_by_pubkey_on_db": AsyncMock(return_value=None),
        "resolve_preset_params": AsyncMock(
            return_value=(SimpleNamespace(value="default"), MagicMock())
        ),
        "create_brainstorm_request_on_db": AsyncMock(
            return_value=SimpleNamespace(private_id=1)
        ),
        "count_brainstorm_requests_with_priority_over_one_on_db": AsyncMock(
            return_value=0
        ),
        "brainstorm_request_db_obj_to_schema_converter": MagicMock(
            return_value=SimpleNamespace(private_id=1)
        ),
        "update_last_time_triggered_graperank_on_db": AsyncMock(),
        "update_brainstorm_request_status_by_id_on_db": _set_status,
        "enqueue_calc_request": _enqueue,
    }.items():
        monkeypatch.setattr(service, name, value)

    return events, db


def _run(db):
    asyncio.run(
        service.create_brainstorm_request(
            db, "graperank", "a" * 64, "a" * 64, nsec_exists=True
        )
    )


def test_the_request_is_committed_before_it_is_enqueued(monkeypatch):
    events, db = _create(monkeypatch, lambda: None)
    _run(db)
    assert events == ["commit", "enqueue"]


def test_a_failed_enqueue_fails_the_committed_request(monkeypatch):
    def _redis_down():
        raise ConnectionError("redis down")

    events, db = _create(monkeypatch, _redis_down)
    with pytest.raises(ConnectionError):
        _run(db)
    assert events == [
        "commit",
        "enqueue",
        (1, BrainstormRequestStatus.FAILURE),
        "commit",
    ]
