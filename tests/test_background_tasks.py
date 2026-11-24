"""A background task that ends is logged and stops the server, except at shutdown."""
import asyncio

import pytest

from app.core import background_tasks
from app.core.background_tasks import BackgroundTasks


@pytest.fixture
def logged(monkeypatch):
    lines: list[tuple[str, bool]] = []

    class Recorder:
        def critical(self, message, exc_info=False):
            lines.append((message, exc_info))

        def info(self, message):
            pass

    monkeypatch.setattr(background_tasks, "logger", Recorder())
    return lines


def _run(scenario):
    deaths: list[str] = []

    async def wrapped():
        await scenario(BackgroundTasks(on_death=lambda: deaths.append("died")))

    asyncio.run(wrapped())
    return deaths


async def _forever():
    await asyncio.sleep(3600)


def test_a_task_that_raises_stops_the_server(logged):
    async def scenario(background):
        async def consumer():
            raise RuntimeError("boom")

        task = background.start("ingest consumer", consumer())
        with pytest.raises(RuntimeError):
            await task

    assert _run(scenario) == ["died"]
    assert logged == [
        ("background task ingest consumer died; stopping the server", True)
    ]


def test_a_task_cancelled_from_inside_stops_the_server(logged):
    """What a stray cancellation does: no `except Exception` sees it."""

    async def scenario(background):
        async def consumer():
            try:
                raise asyncio.CancelledError()
            except Exception:
                pass

        task = background.start("ingest consumer", consumer())
        with pytest.raises(asyncio.CancelledError):
            await task

    assert _run(scenario) == ["died"]
    assert len(logged) == 1 and logged[0][1]


def test_shutdown_cancels_without_stopping_the_server():
    async def scenario(background):
        task = background.start("scheduler", _forever())
        await asyncio.sleep(0)
        background.cancel_all()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert _run(scenario) == []


def test_a_task_that_returns_is_not_a_death():
    async def scenario(background):
        async def switched_off():
            return

        await background.start("billing sync cronjob", switched_off())

    assert _run(scenario) == []
