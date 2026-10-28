"""Your Assistant's Treasure Map: what it should list, and publishing it.

The Map is the caller's Assistant's own kind 10040, naming itself for the duties
it is publishing now (a self-Map; the grammar is `self_map_build`). What it
should list is read from the caller's own Treasure Map, the Duty Menu's offer
and Brainstorm's records of what has landed and is kept up to date. The server
signs it only when the customer asks (`POST /user/assistantTreasureMap`), under
the key the Duty Menu and Trusted Lists use. Nothing here runs on a timer: when
what is live changes, the UI shows the Map as out of date and the customer
presses Update now. Spec: Brainstorm-UI `docs/assistant/TREASURE-MAP-ASK.md`;
the § numbers are Tapestry's `protocols/drafts/treasure-maps.md`.

A press reads the newest version strictly and replaces only its self-entries:
every other tag and the content stay as found. It goes where § 9.2 "Finding a
self-Map" says: the GET's `relays` (the Assistant's outbox, the relays its
self-entries name, the caller's hints for it), and every relay an earlier
version's self-entries named, written at the internal TA relay for its public
URL, as scores are. A press that answers an error changes nothing, the choice
included: it is saved with the request's transaction. The UI waits 30 s, so
each step has its own budget well inside that.
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
from app.cronjobs.periodic_graperank_trigger import PERIOD_HOURS
from app.db_models import BrainstormNsec
from app.repos.brainstorm_nsec import (
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
    get_scheduling_for_pubkey_on_db,
    set_treasure_map_on_by_pubkey_on_db,
)
from app.repos.scheduling_repo import get_default_scheduling_on_db
from app.schemas.schemas import (
    AssistantTreasureMapCheck,
    PublishedAssistantTreasureMap,
    TreasureMapDutyLine,
)
from app.services.assistant_profile_service import assistant_relay_list
from app.services.designation_service import fetch_latest_designation
from app.services.duty_menu_build import (
    menu_created_at,
    offer_rows,
    publish_targets,
    relay_key,
)
from app.services.duty_menu_service import connect_targets, relay_report, send_event
from app.services.self_map_build import (
    SELF_MAP_KIND,
    ScoresUpkeep,
    SelfMapCheck,
    build_self_map_tags,
    content_of,
    evaluate_self_map,
    is_next_version,
    map_hints,
    newest,
    nothing_to_change,
    scores_upkeep,
    self_entry_relays,
    self_map_relays,
)
from app.utils.client_tag import CLIENT_TAG, client_tag
from app.utils.datetimes import as_naive_utc, utc_now

logger = loggr.get_logger(__name__)

# One press at a time per Assistant: two at once could sign versions with the
# same created_at, and a relay keeps either one (§ 9.2 "When it changes"). Held
# in this process, like the nsec rotation flag; move it into Redis if the
# server scales out.
_presses: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def _one_press_at_a_time(assistant: str) -> AsyncIterator[None]:
    async with _presses.setdefault(assistant, asyncio.Lock()):
        yield


async def _assistant(
    db: AsyncDBSession, user_pubkey: str
) -> tuple[BrainstormNsec, Keys]:
    row, _ = await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
        db, pubkey=user_pubkey
    )
    return row, Keys.parse(secret_key=row.nsec)


async def _upkeep_interval(db: AsyncDBSession, pubkey: str) -> int | None:
    """How often the caller's Scores are re-published, in seconds; None when
    nothing re-publishes them. Read as the scheduler reads it
    (`cronjobs/scheduler.py`): its switch, a default policy to run at all, and
    the caller's policy or the default."""
    house = (settings.periodic_graperank_pubkey or "").strip()
    if house and pubkey == house:
        # The scheduler leaves the House Observer to the periodic trigger.
        return PERIOD_HOURS * 3600
    if not settings.scheduler_enabled or await get_default_scheduling_on_db(db) is None:
        return None
    policy = await get_scheduling_for_pubkey_on_db(db, pubkey)
    if policy is None or not policy.enabled:
        return None
    return policy.schedule_interval_seconds


async def _scores_upkeep(db: AsyncDBSession, row: BrainstormNsec) -> ScoresUpkeep:
    return scores_upkeep(
        as_naive_utc(row.last_time_published_graperank),
        await _upkeep_interval(db, row.pubkey),
        utc_now(),
    )


async def _read_reader_map(user_pubkey: str) -> dict[str, Any] | None:
    """The caller's newest kind 10040, read from our relay as
    `designation_service` reads it; None when they have none. Raises when the
    relay can't be read."""
    event = await fetch_latest_designation(
        settings.nostr_transfer_to_relay, user_pubkey
    )
    if (
        not isinstance(event, dict)
        or event.get("kind") != SELF_MAP_KIND
        or str(event.get("pubkey") or "").lower() != user_pubkey.lower()
    ):
        return None
    return event


def _relays(
    check: SelfMapCheck, map_event: dict[str, Any] | None, assistant: str
) -> list[str]:
    return self_map_relays(
        assistant_relay_list(), check.should_say, map_hints(map_event, assistant)
    )


async def read_assistant_treasure_map(
    db: AsyncDBSession, user_pubkey: str
) -> AssistantTreasureMapCheck:
    row, keys = await _assistant(db, user_pubkey)
    assistant = keys.public_key().to_hex()
    try:
        map_event = await _read_reader_map(user_pubkey)
    except Exception as e:
        logger.warning(
            f"treasure map check for {user_pubkey}: their kind 10040 unread: {e!r}"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Couldn't read your Treasure Map, so your Assistant's Treasure Map "
                "couldn't be checked. Try again in a moment."
            ),
        )
    check = evaluate_self_map(
        map_event, offer_rows(assistant), assistant, await _scores_upkeep(db, row)
    )
    return AssistantTreasureMapCheck(
        assistant=assistant,
        on=bool(row.treasure_map_on),
        duties=[
            TreasureMapDutyLine(
                key=line.key,
                status=line.status,
                relay=line.relay,
                reason=line.reason,
                last_landed_at=line.last_landed_at,
                entries=[list(entry) for entry in line.entries],
            )
            for line in check.duties
        ],
        should_say=[list(entry) for entry in check.should_say],
        relays=_relays(check, map_event, assistant),
    )


def _is_own_version(event: Any, assistant: str) -> bool:
    """The Assistant's own kind 10040, validly signed: a press signs the other
    tags of what it builds on as the Assistant's."""
    if (
        not isinstance(event, dict)
        or event.get("kind") != SELF_MAP_KIND
        or str(event.get("pubkey") or "").lower() != assistant
    ):
        return False
    try:
        return Event.from_json(json.dumps(event)).verify()
    except Exception:
        return False


async def _read_versions(relays: list[str], assistant: str) -> list[dict[str, Any]]:
    """Every version of the Assistant's kind 10040 the relays answer with.
    Strict: when no relay answers, nothing is published, since building on
    nothing could drop what a version holds."""
    targets = publish_targets(relays)
    answers = await asyncio.gather(
        *(fetch_latest_designation(t.write_url, assistant) for t in targets),
        return_exceptions=True,
    )
    answered = False
    versions: list[dict[str, Any]] = []
    for target, answer in zip(targets, answers):
        if isinstance(answer, BaseException):
            logger.warning(
                f"treasure map lookup on {target.url} failed for {assistant}: {answer!r}"
            )
            continue
        answered = True
        if answer is not None and _is_own_version(answer, assistant):
            versions.append(answer)
    if not answered:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't read your Assistant's Treasure Map, so nothing was published.",
        )
    return versions


def _sign(
    keys: Keys,
    assistant: str,
    latest: dict[str, Any] | None,
    target: list[tuple[str, str]],
    versions: list[dict[str, Any]],
) -> tuple[Event, dict[str, Any]]:
    """The next version, signed and checked before anything is sent."""
    tags = build_self_map_tags(latest, assistant, target)
    try:
        event = (
            EventBuilder(kind=Kind(SELF_MAP_KIND), content=content_of(latest))
            .tags([Tag.parse(t) for t in tags] + [client_tag()])
            .custom_created_at(
                Timestamp.from_secs(menu_created_at(int(time.time()), versions))
            )
            # nostr-sdk drops a `p` naming the signer unless told otherwise, and
            # a tag kept from the version found may be one.
            .allow_self_tagging()
            .sign_with_keys(keys)
        )
        signed = json.loads(event.as_json())
        ok = (
            event.verify()
            and signed.get("tags") == [*tags, CLIENT_TAG]
            and is_next_version(signed, latest, assistant, target)
        )
    except Exception as e:
        logger.error(f"treasure map for {assistant} could not be signed: {e!r}")
        ok = False
    if not ok:
        logger.error(
            f"treasure map for {assistant} failed its own check, over {latest}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Your Assistant's Treasure Map didn't pass its own check, "
                "so it wasn't published."
            ),
        )
    return event, signed


async def publish_assistant_treasure_map(
    db: AsyncDBSession, user_pubkey: str, on: bool
) -> PublishedAssistantTreasureMap:
    row, keys = await _assistant(db, user_pubkey)
    assistant = keys.public_key().to_hex()
    async with _one_press_at_a_time(assistant):
        try:
            map_event = await _read_reader_map(user_pubkey)
        except Exception as e:
            logger.warning(
                f"treasure map press for {user_pubkey}: their kind 10040 unread: {e!r}"
            )
            if on:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Couldn't read your Treasure Map, so nothing was published.",
                )
            # Turning off lists nothing: the caller's Map only adds places to send it.
            map_event = None
        check = evaluate_self_map(
            map_event, offer_rows(assistant), assistant, await _scores_upkeep(db, row)
        )
        target = check.should_say if on else []
        relays = _relays(check, map_event, assistant)
        versions = await _read_versions(relays, assistant)
        latest = newest(versions)

        await set_treasure_map_on_by_pubkey_on_db(db, user_pubkey, on)
        if nothing_to_change(latest, assistant, target):
            return PublishedAssistantTreasureMap(
                on=on, published=False, event=None, relays=[]
            )

        event, signed = _sign(keys, assistant, latest, target, versions)
        targets = publish_targets(relays, self_entry_relays(versions, assistant))
        client = Client()
        failures: dict[str, str] = {}
        try:
            connected = await connect_targets(client, targets, failures)
            accepted, refused = await send_event(
                client,
                event,
                [t.write_url for t in targets if relay_key(t.write_url) in connected],
            )
        finally:
            await client.disconnect()

    report = relay_report(targets, accepted, refused, failures)
    for line in report:
        if not line.ok:
            logger.warning(
                f"treasure map for {assistant} not on {line.url}: {line.message}"
            )
    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Your Assistant's Treasure Map didn't reach any relay. Try again in a moment.",
        )
    return PublishedAssistantTreasureMap(
        on=on, published=True, event=signed, relays=report
    )
