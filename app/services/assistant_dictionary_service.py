"""The caller's Brainstorm Assistant's Dictionary: adding community concepts to
it and withdrawing them (`POST /user/assistantDictionary` and
`POST /user/assistantDictionary/withdraw`).

A concept is in the Assistant's Dictionary when the Assistant holds a local
copy of it: a kind-39998 header at the community header's `d`, pointing back
with a `pointer` `b` (`dictionary_copy_build`). The customer presses Add or
Remove in Brainstorm-UI's Settings › Dictionary. Only this server holds the
Assistant's key, the one its Trusted Lists and Duty Menu are signed with.
Nothing here runs on a timer. ADR assistant-dictionary/0001; spec:
Brainstorm-UI `docs/dictionary/ASSISTANT-COPY-ASK.md`.

A press runs in four steps, under one lock per Assistant (D6, D7):

1. the request schema validates the body (422);
2. the community headers and the Assistant's own are read from the dictionary
   relay, strictly: if it doesn't answer, nothing is published (502);
3. every concept is decided and signed, and every event checked, before any
   is sent (500);
4. the events are sent in order, through `assistant_publish`, to the
   dictionary relay and the Assistant's outbox, while the deadline lasts.

A withdraw reads only the Assistant's headers: removing a copy doesn't need
the community header it points at.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import HTTPException, status
from nostr_sdk import (  # type: ignore
    Client,
    Event,
    EventBuilder,
    Keys,
    Kind,
    Tag,
    Timestamp,
)
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.loggr import loggr
from app.repos.brainstorm_nsec import (
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.schemas.schemas import AssistantConceptResult, AssistantDictionaryResult
from app.services.assistant_profile_service import assistant_relay_list
from app.services.assistant_publish import connect_targets, relay_report, send_event
from app.services.dictionary_copy_build import (
    B_DEFERRED,
    HEADER_KIND,
    add_outcome,
    copy_tags,
    next_created_at,
    parse_concept_coordinate,
    withdraw_outcome,
    withdrawn_tags,
)
from app.services.dictionary_relay_reader import RelayDidNotAnswer, fetch_newest_headers
from app.services.duty_menu_build import publish_targets, relay_key

logger = loggr.get_logger(__name__)

# Sends start only this long after the press lock is taken, so a press stays
# inside Brainstorm-UI's 60-second wait: connecting (4 s), two reads (5 s
# each), then sends that start before this, each bounded at 12 s (D6).
SEND_DEADLINE_SECONDS = 40.0

# One press at a time per Assistant: two at once could sign versions of one
# coordinate with the same created_at, and a relay keeps either one. Held in
# this process, as the self-Map's is; move it into Redis if the server scales
# out.
_presses: dict[str, asyncio.Lock] = {}

NOT_READ = "Couldn't read the Dictionary relay, so nothing was published."
NOT_SENT = (
    "Your Assistant's Dictionary change didn't reach any relay. "
    "Try again in a moment."
)
FAILED_CHECK = (
    "Your Assistant's copy didn't pass its own check, so nothing was published."
)
CONFLICT = (
    "Your Assistant already keeps something else under this concept's name, "
    "so it was left as it is."
)
SAME_D = (
    "Another concept in this request has the same name, and your Assistant "
    "keeps only one header per name."
)
OWN = "This concept is your Assistant's own, so it can't hold a copy of it."
NOT_FOUND = (
    "The community's definition of this concept couldn't be found on the "
    "Dictionary relay."
)
OUT_OF_TIME = "Not sent: out of time."
NO_RELAY = "No relay took it."


@asynccontextmanager
async def _one_press_at_a_time(assistant: str) -> AsyncIterator[None]:
    async with _presses.setdefault(assistant, asyncio.Lock()):
        yield


async def _assistant_keys(db: AsyncDBSession, user_pubkey: str) -> Keys:
    nsec_row, _ = await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
        db, pubkey=user_pubkey
    )
    return Keys.parse(secret_key=nsec_row.nsec)


async def add_to_assistant_dictionary(
    db: AsyncDBSession, user_pubkey: str, concepts: list[str]
) -> AssistantDictionaryResult:
    """Have the caller's Assistant copy these community concepts."""
    return await _press(db, user_pubkey, concepts, withdraw=False)


async def withdraw_from_assistant_dictionary(
    db: AsyncDBSession, user_pubkey: str, concepts: list[str]
) -> AssistantDictionaryResult:
    """Have the caller's Assistant republish its copies of these concepts as
    withdrawn."""
    return await _press(db, user_pubkey, concepts, withdraw=True)


async def _press(
    db: AsyncDBSession, user_pubkey: str, concepts: list[str], *, withdraw: bool
) -> AssistantDictionaryResult:
    keys = await _assistant_keys(db, user_pubkey)
    assistant = keys.public_key().to_hex()
    named = _named(concepts)
    async with _one_press_at_a_time(assistant):
        started = asyncio.get_running_loop().time()
        community, held = await _read(assistant, named, withdraw)
        results, signed = _decide_and_sign(
            keys, assistant, named, community, held, withdraw
        )
        if signed:
            await _send(results, signed, started)
            if all(results[i].status == "failed" for i, _ in signed):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY, detail=NOT_SENT
                )
    return AssistantDictionaryResult(assistant=assistant, concepts=results)


def _named(concepts: list[str]) -> list[tuple[str, str, str]]:
    """`(coordinate, author, d)` for each concept, once, in the order given."""
    named: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for coordinate in concepts:
        if coordinate in seen:
            continue
        seen.add(coordinate)
        parsed = parse_concept_coordinate(coordinate)
        if parsed is None:
            raise ValueError(f"not a kind-39998 coordinate: {coordinate!r}")
        named.append((coordinate, *parsed))
    return named


async def _read(
    assistant: str, named: list[tuple[str, str, str]], withdraw: bool
) -> tuple[
    dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], dict[str, Any]]
]:
    """The community headers (for an add) and the Assistant's headers at the
    concepts' `d`s, from the dictionary relay only (D3). A relay that didn't
    answer is a 502; any other error is a bug, and propagates as itself."""
    relay = settings.dictionary_relay
    ds = sorted({d for _, _, d in named})
    authors = sorted({author for _, author, _ in named if author != assistant})
    try:
        community = (
            await fetch_newest_headers(relay, authors, ds)
            if authors and not withdraw
            else {}
        )
        held = await fetch_newest_headers(relay, [assistant], ds)
    except RelayDidNotAnswer as e:
        logger.warning(f"dictionary read on {relay} failed for {assistant}: {e!r}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=NOT_READ
        ) from e
    return community, held


def _decide_and_sign(
    keys: Keys,
    assistant: str,
    named: list[tuple[str, str, str]],
    community: dict[tuple[str, str], dict[str, Any]],
    held: dict[tuple[str, str], dict[str, Any]],
    withdraw: bool,
) -> tuple[list[AssistantConceptResult], list[tuple[int, Event]]]:
    """Each concept's outcome (D4), and the signed, checked event of each that
    publishes, by its place in the results."""
    now = int(time.time())
    results: list[AssistantConceptResult] = []
    signed: list[tuple[int, Event]] = []
    ds_named: set[str] = set()
    for coordinate, author, d in named:
        local = f"{HEADER_KIND}:{assistant}:{d}"
        if d in ds_named:
            results.append(_result(coordinate, local, "conflict", SAME_D))
            continue
        ds_named.add(d)
        if author == assistant:
            results.append(_result(coordinate, local, "conflict", OWN))
            continue
        newest = held.get((assistant, d))
        tags: list[list[str]] | None
        if withdraw:
            outcome: str = withdraw_outcome(newest, coordinate)
            tags = withdrawn_tags(d) if outcome == "withdrawn" else None
        else:
            header = community.get((author, d))
            if header is None:
                results.append(_result(coordinate, None, "not_found", NOT_FOUND))
                continue
            tags = copy_tags(header, coordinate)
            outcome = add_outcome(tags, newest, coordinate)
            if outcome not in ("published", "updated"):
                tags = None
        if outcome == "conflict":
            results.append(_result(coordinate, local, "conflict", CONFLICT))
            continue
        results.append(_result(coordinate, local, outcome))
        if tags is not None:
            event = _sign(keys, tags, next_created_at(now, newest))
            _check(event, assistant, d, coordinate, withdraw)
            signed.append((len(results) - 1, event))
    return results, signed


def _result(
    coordinate: str, local: str | None, outcome: str, error: str | None = None
) -> AssistantConceptResult:
    return AssistantConceptResult(
        community=coordinate,
        local=local,
        status=outcome,  # type: ignore[arg-type]
        error=error,
    )


def _sign(keys: Keys, tags: list[list[str]], created_at: int) -> Event:
    return (
        EventBuilder(kind=Kind(HEADER_KIND), content="")
        .tags([Tag.parse(t) for t in tags])
        .custom_created_at(Timestamp.from_secs(created_at))
        .sign_with_keys(keys)
    )


def _check(
    event: Event, assistant: str, d: str, coordinate: str, withdraw: bool
) -> None:
    """The signed event is what was composed: refuse to send anything else (D5)."""
    signed = json.loads(event.as_json())
    tags = signed.get("tags") or []
    expected_b = ["b", B_DEFERRED] if withdraw else ["b", coordinate, "pointer"]
    if not (
        event.verify()
        and signed.get("kind") == HEADER_KIND
        and signed.get("pubkey") == assistant
        and tags[:1] == [["d", d]]
        and [t for t in tags if t and t[0] == "b"] == [expected_b]
    ):
        logger.error(f"dictionary copy for {assistant} failed its own check: {signed}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=FAILED_CHECK
        )


async def _send(
    results: list[AssistantConceptResult],
    signed: list[tuple[int, Event]],
    started: float,
) -> None:
    """Send each signed event in order while the deadline lasts, filling in its
    concept's event and relay report, or why it wasn't sent (D5, D6)."""
    targets = publish_targets([settings.dictionary_relay], assistant_relay_list())
    loop = asyncio.get_running_loop()
    client = Client()
    try:
        connected = await connect_targets(client, targets)
        urls = [t.write_url for t in targets if relay_key(t.write_url) in connected]
        for index, event in signed:
            result = results[index]
            if loop.time() - started >= SEND_DEADLINE_SECONDS:
                results[index] = result.model_copy(
                    update={"status": "failed", "error": OUT_OF_TIME}
                )
                continue
            accepted, refused = await send_event(client, event, urls)
            report = relay_report(targets, connected, accepted, refused)
            for line in report:
                if not line.ok:
                    # Quoted: the `d` is the caller's text and the message the
                    # relay's, and either may hold a newline.
                    logger.warning(
                        f"dictionary copy {result.local!r} not on {line.url!r}: "
                        f"{line.message!r}"
                    )
            if accepted:
                results[index] = result.model_copy(
                    update={"event": json.loads(event.as_json()), "relays": report}
                )
            else:
                results[index] = result.model_copy(
                    update={"status": "failed", "error": NO_RELAY, "relays": report}
                )
    finally:
        await client.disconnect()
