"""One websocket per relay, opened once, for a read by quorum and then a send
on the same sockets: the Assistant's AI keys (`agent_keys_service`).

- `read_quorum` asks every relay for the newest event of a filter and returns
  once more than half have answered, plus `QUORUM_GRACE_SECONDS` for the rest,
  instead of waiting for the slowest (or a dead relay's timeout). A majority
  read still meets every write a majority took, which is what NIP-FA's rule is
  for. A relay that times out, fails, or ends the REQ with `CLOSED` hasn't
  answered: it never reads as one holding nothing.
- `send` publishes on the sockets the read used, to every relay that
  connected, including those whose answer came too late to wait for: one
  connect per relay per request, not two.

Raw websockets, as `designation_service.fetch_latest`, because nostr-sdk's
per-relay fetch returns an empty list both for a relay that timed out and for
one that sent `CLOSED`, so a quorum can't be counted with it.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import websockets

from app.core.loggr import loggr
from app.schemas.schemas import AssistantRelayResult
from app.services.duty_menu_build import RelayTarget, relay_key

logger = loggr.get_logger(__name__)

CONNECT_TIMEOUT_SECONDS = 4.0
# From the start of the read to the last answer waited for, connect included.
READ_TIMEOUT_SECONDS = 5.0
# How long, once a majority has answered, the read still waits for the rest.
QUORUM_GRACE_SECONDS = 0.5
# Each relay's `OK`, the sends running side by side.
SEND_TIMEOUT_SECONDS = 10.0
CLOSE_TIMEOUT_SECONDS = 1.0
NOT_REACHED = "couldn't connect"

_SUB_ID = "quorum"


def _newer(event: dict, than: dict | None) -> bool:
    """NIP-01's order for replaceable events: later `created_at`, then lower id."""
    if than is None:
        return True
    at, than_at = event.get("created_at", 0), than.get("created_at", 0)
    return at > than_at or (at == than_at and event.get("id", "") < than.get("id", ""))


async def _open(url: str) -> Any:
    return await websockets.connect(
        url, open_timeout=CONNECT_TIMEOUT_SECONDS, close_timeout=CLOSE_TIMEOUT_SECONDS
    )


class RelayGroup:
    """`async with RelayGroup(targets) as relays:` connects to every target at
    once, in the background; leaving closes them all."""

    def __init__(self, targets: list[RelayTarget]) -> None:
        self.targets = targets
        self._connects: dict[str, asyncio.Task] = {}

    async def __aenter__(self) -> RelayGroup:
        for target in self.targets:
            self._connects[relay_key(target.write_url)] = asyncio.create_task(
                _open(target.write_url)
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        closing = []
        for task in self._connects.values():
            if not task.done():
                task.cancel()
            elif not task.cancelled() and task.exception() is None:
                closing.append(task.result().close())
        await asyncio.gather(*self._connects.values(), return_exceptions=True)
        await asyncio.gather(*closing, return_exceptions=True)

    async def _socket(self, target: RelayTarget) -> Any:
        """The target's open socket. Shielded: a read given up on must not
        cancel the connect the send still needs."""
        return await asyncio.shield(self._connects[relay_key(target.write_url)])

    async def _latest(
        self, target: RelayTarget, flt: dict, own: Callable[[dict], bool]
    ) -> dict | None:
        ws = await self._socket(target)
        await ws.send(json.dumps(["REQ", _SUB_ID, flt]))
        latest: dict | None = None
        while True:
            msg = json.loads(await ws.recv())
            if not isinstance(msg, list) or len(msg) < 2 or msg[1] != _SUB_ID:
                continue
            if msg[0] == "EVENT" and len(msg) >= 3:
                event = msg[2]
                if isinstance(event, dict) and _newer(event, latest) and own(event):
                    latest = event
            elif msg[0] == "EOSE":
                break
            elif msg[0] == "CLOSED":
                raise RuntimeError(f"relay closed the request: {msg[2:]!r}")
        try:
            await ws.send(json.dumps(["CLOSE", _SUB_ID]))
        except Exception:
            pass
        return latest

    async def read_quorum(
        self, flt: dict, own: Callable[[dict], bool], name: str
    ) -> dict[str, dict | None]:
        """What each relay that answered holds of `flt`, by `relay_key`: its
        newest `own` event, or None. Returns once more than half have answered
        and the grace has passed, or at the deadline; whoever hasn't answered
        by then is left out."""
        reads = {
            asyncio.create_task(self._latest(target, flt, own)): target
            for target in self.targets
        }
        held: dict[str, dict | None] = {}
        majority = len(self.targets) // 2 + 1
        loop = asyncio.get_running_loop()
        until = loop.time() + READ_TIMEOUT_SECONDS
        pending = set(reads)
        try:
            while pending:
                left = until - loop.time()
                if left <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending, timeout=left, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    target = reads[task]
                    error = task.exception()
                    if error is not None:
                        logger.warning(
                            f"{name} lookup on {target.url} failed: {error!r}"
                        )
                        continue
                    held[relay_key(target.write_url)] = task.result()
                if len(held) >= majority:
                    until = min(until, loop.time() + QUORUM_GRACE_SECONDS)
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for task in pending:
            logger.info(f"{name} lookup on {reads[task].url}: not waited for")
        return held

    async def _publish(self, target: RelayTarget, event: dict) -> tuple[bool, str]:
        ws = await self._socket(target)
        await ws.send(json.dumps(["EVENT", event]))
        while True:
            msg = json.loads(await ws.recv())
            if (
                isinstance(msg, list)
                and len(msg) >= 3
                and msg[0] == "OK"
                and msg[1] == event["id"]
            ):
                return msg[2] is True, str(msg[3]) if len(msg) > 3 else ""

    async def send(
        self, event: dict, name: str, author: str
    ) -> list[AssistantRelayResult]:
        """Send `event` to every target that connected; one report line per
        target, by its public address: took it, or why not."""

        async def one(target: RelayTarget) -> AssistantRelayResult:
            try:
                await self._socket(target)
            except Exception:
                return AssistantRelayResult(
                    url=target.url, ok=False, message=NOT_REACHED
                )
            try:
                ok, message = await asyncio.wait_for(
                    self._publish(target, event), SEND_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                return AssistantRelayResult(url=target.url, ok=False, message="timeout")
            except Exception as e:
                logger.warning(f"send to {target.url} failed: {e!r}")
                return AssistantRelayResult(
                    url=target.url, ok=False, message="not sent"
                )
            if ok:
                return AssistantRelayResult(url=target.url, ok=True)
            return AssistantRelayResult(
                url=target.url, ok=False, message=message or "refused"
            )

        report = list(await asyncio.gather(*(one(t) for t in self.targets)))
        for line in report:
            if not line.ok:
                logger.warning(f"{name} for {author} not on {line.url}: {line.message}")
        return report
