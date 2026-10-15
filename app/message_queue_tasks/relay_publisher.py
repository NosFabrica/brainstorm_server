"""Publish pre-signed events to one relay over raw websockets, confirmed by `OK`.

Events stream in from an async iterator and fan out over a few connections.
Each connection tracks its unacked events; an event is done only once the relay
answers `["OK", id, true, ...]`. `false` OKs are retried with backoff, and a
dropped or silent connection resends its unacked events on a new one (resending
is safe: the relay answers `duplicate:`). Only the unresolved events are held in
memory, capped by `PublishConfig.max_outstanding`.
"""

import asyncio
import json
import time
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import websockets

from app.core.loggr import loggr
from app.message_queue_tasks.ta_signing import SignedEvent

logger = loggr.get_logger(__name__)


@dataclass(frozen=True)
class PublishConfig:
    connections: int = 4
    max_in_flight: int = 2000  # unacked events per connection
    max_outstanding: int = 10_000  # queued + in flight + awaiting retry
    ack_timeout_s: float = 30.0  # no OK for this long with events in flight → reconnect
    max_event_attempts: int = 5  # false OKs before an event counts as failed
    max_connect_attempts: int = 5  # consecutive connections without progress
    retry_base_delay_s: float = 0.5
    open_timeout_s: float = 10.0
    close_timeout_s: float = 2.0
    yield_every: int = 50


@dataclass
class PublishStats:
    n_events: int = 0
    n_acked: int = 0
    n_failed: int = 0
    n_retried: int = 0
    n_reconnects: int = 0
    aborted: bool = False  # every connection gave up; not all events were produced
    rejected: Counter[str] = field(default_factory=Counter)
    failure_samples: list[str] = field(default_factory=list)
    t_first_send: float | None = None
    t_last_ack: float | None = None

    @property
    def n_unacked(self) -> int:
        return self.n_events - self.n_acked

    @property
    def t_acked(self) -> float:
        if self.t_first_send is None or self.t_last_ack is None:
            return 0.0
        return round(self.t_last_ack - self.t_first_send, 3)


@dataclass(slots=True)
class _Item:
    id: str
    frame: str
    attempts: int = 0


class _AckTimeout(Exception):
    pass


def _reject_reason(message: str) -> str:
    # NIP-01 machine-readable prefix: "rate-limited: ...", "invalid: ...".
    prefix, sep, _ = message.partition(":")
    return prefix.strip() if sep and prefix.strip() else "unknown"


class _Publisher:
    def __init__(self, url: str, config: PublishConfig) -> None:
        self.url = url
        self.config = config
        self.stats = PublishStats()
        self.todo: asyncio.Queue[_Item | None] = asyncio.Queue()
        self.outstanding = asyncio.Semaphore(config.max_outstanding)
        self.done = asyncio.Event()
        self.produced_all = False
        self.n_resolved = 0
        self.dead_workers = 0

    async def run(self, events: AsyncIterator[SignedEvent]) -> PublishStats:
        producer = asyncio.create_task(self._produce(events))
        workers = [
            asyncio.create_task(self._worker()) for _ in range(self.config.connections)
        ]
        try:
            await self.done.wait()
            if producer.done() and producer.exception():
                raise producer.exception()  # type: ignore[misc]
        finally:
            for task in (producer, *workers):
                task.cancel()
            await asyncio.gather(producer, *workers, return_exceptions=True)
        self.stats.n_failed = self.stats.n_events - self.stats.n_acked
        return self.stats

    async def _produce(self, events: AsyncIterator[SignedEvent]) -> None:
        try:
            async for event in events:
                await self.outstanding.acquire()
                self.stats.n_events += 1
                self.todo.put_nowait(_Item(event.id, f'["EVENT",{event.json}]'))
        except BaseException:
            self.done.set()
            raise
        self.produced_all = True
        self._check_done()

    def _resolve(self) -> None:
        self.n_resolved += 1
        self.outstanding.release()
        self._check_done()

    def _check_done(self) -> None:
        if self.produced_all and self.n_resolved == self.stats.n_events:
            self._finish()

    def _finish(self) -> None:
        if not self.done.is_set():
            self.done.set()
            for _ in range(self.config.connections):
                self.todo.put_nowait(None)

    def _requeue(self, pending: dict[str, _Item]) -> None:
        for item in pending.values():
            self.todo.put_nowait(item)
        pending.clear()

    def _requeue_later(self, item: _Item) -> None:
        delay = self.config.retry_base_delay_s * 2 ** (item.attempts - 1)
        asyncio.get_running_loop().call_later(delay, self.todo.put_nowait, item)

    async def _worker(self) -> None:
        failures = 0
        while not self.done.is_set():
            pending: dict[str, _Item] = {}
            progress = [False]
            try:
                async with websockets.connect(
                    self.url,
                    compression=None,
                    max_size=None,
                    open_timeout=self.config.open_timeout_s,
                    close_timeout=self.config.close_timeout_s,
                ) as ws:
                    try:
                        await self._run_connection(ws, pending, progress)
                    finally:
                        # Before the close handshake, which can take close_timeout_s.
                        self._requeue(pending)
            except Exception as e:
                if not self.done.is_set():
                    logger.warning(
                        f"relay publish: connection to {self.url} lost: {e!r}"
                    )
            if self.done.is_set():
                return
            failures = 0 if progress[0] else failures + 1
            if failures >= self.config.max_connect_attempts:
                self.dead_workers += 1
                logger.error(
                    f"relay publish: giving up on a connection to {self.url} after "
                    f"{failures} attempts without progress"
                )
                if self.dead_workers == self.config.connections:
                    self.stats.aborted = True
                    self._finish()
                return
            self.stats.n_reconnects += 1
            await asyncio.sleep(
                self.config.retry_base_delay_s * 2 ** max(failures - 1, 0)
            )

    async def _run_connection(
        self, ws, pending: dict[str, _Item], progress: list[bool]
    ) -> None:
        in_flight = asyncio.Semaphore(self.config.max_in_flight)
        tasks = [
            asyncio.create_task(self._send(ws, pending, in_flight)),
            asyncio.create_task(self._read(ws, pending, in_flight, progress)),
            asyncio.create_task(self.done.wait()),
        ]
        try:
            finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in finished:
            if not task.cancelled() and task.exception():
                raise task.exception()  # type: ignore[misc]

    async def _send(
        self, ws, pending: dict[str, _Item], in_flight: asyncio.Semaphore
    ) -> None:
        sent = 0
        while True:
            await in_flight.acquire()
            item = await self.todo.get()
            if item is None:
                return
            pending[item.id] = item
            if self.stats.t_first_send is None:
                self.stats.t_first_send = time.perf_counter()
            await ws.send(item.frame)
            sent += 1
            if sent % self.config.yield_every == 0:
                await asyncio.sleep(0)

    async def _read(
        self,
        ws,
        pending: dict[str, _Item],
        in_flight: asyncio.Semaphore,
        progress: list[bool],
    ) -> None:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), self.config.ack_timeout_s)
            except TimeoutError:
                if pending:
                    raise _AckTimeout(
                        f"no OK for {self.config.ack_timeout_s}s with {len(pending)} in flight"
                    )
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, list) or not msg:
                continue
            if msg[0] == "NOTICE":
                logger.warning(f"relay publish: NOTICE from {self.url}: {msg[1:]}")
                continue
            if msg[0] != "OK" or len(msg) < 3:
                continue
            item = pending.pop(msg[1], None)
            if item is None:
                continue
            in_flight.release()
            progress[0] = True
            if msg[2] is True:
                self.stats.n_acked += 1
                self.stats.t_last_ack = time.perf_counter()
                self._resolve()
                continue
            reason = str(msg[3]) if len(msg) > 3 else ""
            self.stats.rejected[_reject_reason(reason)] += 1
            item.attempts += 1
            if item.attempts >= self.config.max_event_attempts:
                if len(self.stats.failure_samples) < 5:
                    self.stats.failure_samples.append(f"{item.id}: {reason}")
                self._resolve()
            else:
                self.stats.n_retried += 1
                self._requeue_later(item)


async def publish_events(
    url: str,
    events: AsyncIterator[SignedEvent],
    config: PublishConfig = PublishConfig(),
) -> PublishStats:
    """Send every event and wait for the relay's OK on each. Delivery is complete
    only when `not stats.aborted and stats.n_unacked == 0`; otherwise some events
    never got a `true` OK (retries exhausted, or the relay stayed unreachable)."""
    return await _Publisher(url, config).run(events)
