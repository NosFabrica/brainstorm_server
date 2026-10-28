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

A press reads the newest version strictly and publishes the self-entries the
GET says, with the `client` tag and empty content: only this server signs the
Assistant's kind 10040, so a version holds nothing else to keep. It reads and
sends only where § 9.2 "Finding a self-Map" sends a writer: the Assistant's
outbox, the relays its self-entries name, and every relay an earlier version's
self-entries named, the TA relay at its internal URL, as scores are. The
caller's hints for the Assistant are in the GET's `relays`, for readers, and a
press never connects to them: the caller writes their own Map, so a hint can
name any host. Signing and sending are `assistant_publish`'s, shared with the
Duty Menu.

The choice (`brainstorm_nsec.treasure_map_on`) is saved only by a press that
succeeds, after its publish, and committed before the answer reports it. A
press that answers an error writes nothing.

The UI waits 30 s. A press takes about 28 s at worst: the caller's Map and the
versions are read in up to 6 s each, the connect takes up to 4 s and the send
12 s. A second press for the same Assistant is refused while one is under way.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Iterator

from fastapi import HTTPException, status
from nostr_sdk import Event  # type: ignore
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.loggr import loggr
from app.cronjobs.periodic_graperank_trigger import PERIOD_HOURS
from app.cronjobs.scheduler import has_grapevine
from app.db_models import BrainstormNsec
from app.repos.brainstorm_nsec import set_treasure_map_on_by_pubkey_on_db
from app.repos.scheduling_repo import (
    get_default_scheduling_on_db,
    get_scheduling_on_db,
)
from app.schemas.schemas import (
    AssistantTreasureMapCheck,
    PublishedAssistantTreasureMap,
    TreasureMapDutyLine,
)
from app.services.assistant_profile_service import assistant_relay_list
from app.services.assistant_publish import (
    assistant,
    is_own,
    reached_some,
    read_newest,
    send_to,
    sign_checked,
)
from app.services.designation_service import fetch_latest_designation
from app.services.duty_menu_build import (
    RelayTarget,
    menu_created_at,
    offer_rows,
    publish_targets,
    relay_key,
)
from app.services.self_map_build import (
    MAX_MAP_KEYS,
    SELF_MAP_KIND,
    ScoresUpkeep,
    SelfMapCheck,
    evaluate_self_map,
    newest,
    nothing_to_change,
    read_self_entries,
    same_self_entries,
    scores_upkeep,
    self_entry_relays,
    self_map_relays,
    self_map_tags,
    too_big_to_check,
)
from app.utils.datetimes import as_naive_utc, utc_now

logger = loggr.get_logger(__name__)

MAP = "Treasure Map"

# The Assistants with a press under way. One press at a time per Assistant: two
# at once could sign versions with the same created_at, and a relay keeps either
# one (§ 9.2 "When it changes"). A second is refused, not queued: queued, it
# would answer only after both had run, past the UI's 30 s, holding a pooled
# database connection all the while. An Assistant leaves the set when its press
# ends. Held in this process, like the nsec rotation flag; move it into Redis if
# the server scales out.
_pressing: set[str] = set()


@contextmanager
def _one_press_at_a_time(assistant: str) -> Iterator[None]:
    if assistant in _pressing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Your Assistant's Treasure Map is already being published. "
                "Try again in a moment."
            ),
        )
    _pressing.add(assistant)
    try:
        yield
    finally:
        _pressing.discard(assistant)


async def _upkeep_interval(db: AsyncDBSession, row: BrainstormNsec) -> int | None:
    """How often the caller's Scores are re-published, in seconds; None when
    nothing re-publishes them. Read as the scheduler admits a run
    (`cronjobs/scheduler.py`): its switch, a default policy to run at all, the
    caller's policy or the default, enabled, and a grapevine to score, by the
    scheduler's own `has_grapevine`."""
    house = (settings.periodic_graperank_pubkey or "").strip()
    if house and row.pubkey == house:
        # The scheduler leaves the House Observer to the periodic trigger.
        return PERIOD_HOURS * 3600
    if not settings.scheduler_enabled:
        return None
    default = await get_default_scheduling_on_db(db)
    if default is None:
        return None
    own = (
        await get_scheduling_on_db(db, row.scheduling_id)
        if row.scheduling_id is not None
        else None
    )
    policy = own or default
    if not policy.enabled:
        return None
    try:
        grapevine = await has_grapevine(row.pubkey)
    except Exception as e:
        logger.warning(f"follow count for {row.pubkey} unread: {e!r}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Couldn't check whether your Scores are kept up to date. "
                "Try again in a moment."
            ),
        )
    return policy.schedule_interval_seconds if grapevine else None


async def _scores_upkeep(db: AsyncDBSession, row: BrainstormNsec) -> ScoresUpkeep:
    return scores_upkeep(
        as_naive_utc(row.last_time_published_graperank),
        await _upkeep_interval(db, row),
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


def _too_big(what_follows: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=(
            f"Your Treasure Map names more than {MAX_MAP_KEYS} keys, too many to "
            f"check, so {what_follows}."
        ),
    )


def _relays(
    check: SelfMapCheck, map_event: dict[str, Any] | None, assistant: str
) -> list[str]:
    """Where a reader looks for the Map (§ 9.2): the writer's relays, then the
    caller's hints for the Assistant."""
    return self_map_relays(
        assistant_relay_list(),
        check.should_say,
        self_entry_relays([map_event], assistant),
    )


def _writer_relays(check: SelfMapCheck) -> list[str]:
    """Where a press reads and sends, before the relays earlier versions name:
    the Assistant's outbox and the relays its self-entries name."""
    return self_map_relays(assistant_relay_list(), check.should_say, [])


def _missing(
    targets: list[RelayTarget], held: dict[str, dict | None], latest: dict[str, Any]
) -> list[RelayTarget]:
    """The targets read that don't hold `latest`: no answer, none, or another
    version."""
    return [
        t
        for t in targets
        if (held.get(relay_key(t.write_url)) or {}).get("id") != latest.get("id")
    ]


async def _save_choice(db: AsyncDBSession, user_pubkey: str, on: bool) -> None:
    """Remember the choice, committed here: `get_db` commits only after the
    response is sent, which would answer 200 before the choice was durable and
    keep the caller's row locked while it went out."""
    await set_treasure_map_on_by_pubkey_on_db(db, user_pubkey, on)
    await db.commit()


async def read_assistant_treasure_map(
    db: AsyncDBSession, user_pubkey: str
) -> AssistantTreasureMapCheck:
    row, keys = await assistant(db, user_pubkey)
    who = keys.public_key().to_hex()
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
    if too_big_to_check(map_event):
        raise _too_big("your Assistant's Treasure Map couldn't be checked")
    check = evaluate_self_map(
        map_event, offer_rows(who), who, await _scores_upkeep(db, row)
    )
    return AssistantTreasureMapCheck(
        assistant=who,
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
        relays=_relays(check, map_event, who),
    )


async def publish_assistant_treasure_map(
    db: AsyncDBSession, user_pubkey: str, on: bool
) -> PublishedAssistantTreasureMap:
    row, keys = await assistant(db, user_pubkey)
    who = keys.public_key().to_hex()
    with _one_press_at_a_time(who):
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
            # Turning off lists nothing: the caller's Map only adds the relays
            # its live duties name.
            map_event = None
        if too_big_to_check(map_event):
            if on:
                raise _too_big("nothing was published")
            map_event = None
        check = evaluate_self_map(
            map_event, offer_rows(who), who, await _scores_upkeep(db, row)
        )
        target = check.should_say if on else []
        relays = _writer_relays(check)
        read = publish_targets(relays)
        held = await read_newest(
            read,
            {"kinds": [SELF_MAP_KIND], "authors": [who], "limit": 1},
            lambda e: is_own(e, who, SELF_MAP_KIND),
            MAP,
        )
        versions = [e for e in held.values() if e is not None]
        latest = newest(versions)

        if nothing_to_change(latest, who, target):
            missing = _missing(read, held, latest) if latest is not None else []
            if not missing:
                await _save_choice(db, user_pubkey, on)
                return PublishedAssistantTreasureMap(
                    on=on, published=False, event=None, relays=[]
                )
            # A relay missed it, such as the TA relay while it was down: the
            # version already signed goes there, unchanged.
            report = await send_to(
                Event.from_json(json.dumps(latest)), missing, MAP, who
            )
            await _save_choice(db, user_pubkey, on)
            return PublishedAssistantTreasureMap(
                on=on, published=True, event=latest, relays=report
            )

        event, signed = sign_checked(
            keys,
            SELF_MAP_KIND,
            "",
            self_map_tags(who, target),
            menu_created_at(int(time.time()), versions),
            lambda s: same_self_entries(read_self_entries(s, who), target),
            MAP,
        )
        targets = publish_targets(relays, self_entry_relays(versions, who))
        report = reached_some(await send_to(event, targets, MAP, who), MAP)
        await _save_choice(db, user_pubkey, on)
    return PublishedAssistantTreasureMap(
        on=on, published=True, event=signed, relays=report
    )
