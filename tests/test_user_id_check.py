"""The nightly count of user nodes without a user_id."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.core.config import settings
from app.cronjobs import user_id_check as mod


def _driver(monkeypatch):
    class _Ctx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mod.neo4j_driver, "session", MagicMock(return_value=_Ctx()))


def test_it_does_not_run_while_ids_are_off(monkeypatch):
    monkeypatch.setattr(settings, "user_ids_enabled", False)
    check = AsyncMock()
    monkeypatch.setattr(mod, "check_user_ids", check)
    asyncio.run(asyncio.wait_for(mod.user_id_check_cronjob(), timeout=1))
    check.assert_not_awaited()


def test_nodes_without_an_id_are_warned_about_with_the_number(monkeypatch):
    _driver(monkeypatch)
    monkeypatch.setattr(
        mod, "count_nodes_without_user_id", AsyncMock(return_value=(7, 2))
    )
    warning = MagicMock()
    monkeypatch.setattr(mod.logger, "warning", warning)
    assert asyncio.run(mod.check_user_ids()) == 7
    assert warning.call_args.args[0].startswith("7 NostrUser nodes have no user_id")


def test_only_malformed_pubkeys_left_is_silent(monkeypatch):
    _driver(monkeypatch)
    monkeypatch.setattr(
        mod, "count_nodes_without_user_id", AsyncMock(return_value=(0, 3))
    )
    warning = MagicMock()
    monkeypatch.setattr(mod.logger, "warning", warning)
    assert asyncio.run(mod.check_user_ids()) == 0
    warning.assert_not_called()


def test_a_failing_check_does_not_end_the_loop(monkeypatch):
    monkeypatch.setattr(settings, "user_ids_enabled", True)
    calls = []

    async def failing():
        calls.append(1)
        raise ConnectionError("neo4j down")

    async def stop_after_two(_seconds):
        if len(calls) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(mod, "check_user_ids", failing)
    monkeypatch.setattr(mod.asyncio, "sleep", stop_after_two)
    try:
        asyncio.run(mod.user_id_check_cronjob())
    except asyncio.CancelledError:
        pass
    assert len(calls) == 2
