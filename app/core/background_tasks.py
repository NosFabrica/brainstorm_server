"""The server's long-running tasks, watched.

A consumer or cronjob that ends takes its queue or schedule with it while the
API keeps answering, and a task nobody awaits ends without a log line. Here its
end is logged with the traceback and the process is stopped, so the pod
restarts with every task running again.
"""
import asyncio
import os
import signal
from collections.abc import Callable, Coroutine

from app.core.loggr import loggr

logger = loggr.get_logger(__name__)

# After asking uvicorn to stop, how long before the process is ended anyway.
_EXIT_GRACE_SECONDS = 30


def _stop_process() -> None:
    os.kill(os.getpid(), signal.SIGTERM)
    asyncio.get_running_loop().call_later(_EXIT_GRACE_SECONDS, os._exit, 1)


class BackgroundTasks:
    def __init__(self, on_death: Callable[[], None] = _stop_process):
        self._on_death = on_death
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    def start(self, name: str, coro: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(self._watched(name, coro), name=name)
        self._tasks.append(task)
        return task

    async def _watched(self, name: str, coro: Coroutine) -> None:
        try:
            await coro
        except BaseException:
            if not self._stopping:
                # Caught here, where the traceback still leads to the line it died on.
                logger.critical(
                    f"background task {name} died; stopping the server", exc_info=True
                )
                self._on_death()
            raise
        # A cronjob that is switched off returns at once: not a death.
        logger.info(f"background task {name} returned")

    def cancel_all(self) -> None:
        """For shutdown: from here an ending task is expected."""
        self._stopping = True
        for task in self._tasks:
            task.cancel()
