"""Signing and publishing the Assistant's own events: its Duty Menu and Your
Assistant's Treasure Map.

Both are signed with the caller's Assistant key, the one its Trusted Assertions
and Trusted Lists are signed with, and both are published the same way: read
the newest version at each relay strictly, sign the next one and check it
before anything is sent, connect once to every target, send, and report per
relay. The TA relay's public URL is written at its internal URL, as scores are
(`duty_menu_build.publish_targets`).

The UI waits 30 s for a publish. The read takes up to 6 s (`fetch_latest`'s
5 s and a 1 s close), the connect 4 s and the send 12 s.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any, Callable

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

from app.core.loggr import loggr
from app.db_models import BrainstormNsec
from app.repos.brainstorm_nsec import (
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.schemas.schemas import AssistantRelayResult
from app.services.designation_service import fetch_latest
from app.services.duty_menu_build import RelayTarget, relay_key
from app.utils.client_tag import CLIENT_TAG, client_tag

logger = loggr.get_logger(__name__)

CONNECT_TIMEOUT = timedelta(seconds=4)
# nostr-sdk gives each relay 10 s for its OK; this only bounds the call itself.
SEND_TIMEOUT_SECONDS = 12
# What a report says of a relay that couldn't be reached. The transport's own
# error is logged, not answered: it can describe a host the caller can't see.
NOT_REACHED = "couldn't connect"


async def assistant(db: AsyncDBSession, user_pubkey: str) -> tuple[BrainstormNsec, Keys]:
    """The caller's Assistant: its row, and the key it signs with."""
    row, _ = await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
        db, pubkey=user_pubkey
    )
    return row, Keys.parse(secret_key=row.nsec)


def is_own(event: Any, author: str, kind: int) -> bool:
    """Is `event` the author's own event of `kind`, validly signed?"""
    if (
        not isinstance(event, dict)
        or event.get("kind") != kind
        or str(event.get("pubkey") or "").lower() != author.lower()
    ):
        return False
    try:
        return Event.from_json(json.dumps(event)).verify()
    except Exception:
        return False


async def read_newest(
    targets: list[RelayTarget],
    flt: dict,
    own: Callable[[dict], bool],
    name: str,
) -> dict[str, dict | None]:
    """What each target holds of `flt`, by `relay_key` of where it's read: its
    newest event when that is `own`, else None. A target that doesn't answer is
    left out. Strict: when none answers, nothing is published (502), since an
    unread version could be dated ahead of now or name relays the publish would
    then miss."""
    answers = await asyncio.gather(
        *(fetch_latest(t.write_url, flt) for t in targets), return_exceptions=True
    )
    held: dict[str, dict | None] = {}
    for target, answer in zip(targets, answers):
        if isinstance(answer, BaseException):
            logger.warning(f"{name} lookup on {target.url} failed: {answer!r}")
            continue
        held[relay_key(target.write_url)] = (
            answer if answer is not None and own(answer) else None
        )
    if not held:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Couldn't read your Assistant's {name}, so nothing was published.",
        )
    return held


def sign_checked(
    keys: Keys,
    kind: int,
    content: str,
    tags: list[list[str]],
    created_at: int,
    check: Callable[[dict], bool],
    name: str,
    self_tagging: bool = False,
) -> tuple[Event, dict[str, Any]]:
    """`tags` and the client tag, signed with `keys` and checked before anything
    is sent: the event verifies, carries exactly those tags, and passes `check`.
    A 500 otherwise. With `self_tagging`, a `p` naming the signer is kept, which
    nostr-sdk otherwise drops."""
    who = keys.public_key().to_hex()
    try:
        builder = (
            EventBuilder(kind=Kind(kind), content=content)
            .tags([Tag.parse(t) for t in tags] + [client_tag()])
            .custom_created_at(Timestamp.from_secs(created_at))
        )
        if self_tagging:
            builder = builder.allow_self_tagging()
        event = builder.sign_with_keys(keys)
        signed = json.loads(event.as_json())
        ok = (
            event.verify()
            and signed.get("tags") == [*tags, CLIENT_TAG]
            and check(signed)
        )
    except Exception as e:
        logger.error(f"{name} for {who} could not be signed: {e!r}")
        ok = False
    if not ok:
        logger.error(f"{name} for {who} failed its own check")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Your Assistant's {name} didn't pass its own check, "
                "so it wasn't published."
            ),
        )
    return event, signed


async def connect_targets(client: Client, targets: list[RelayTarget]) -> set[str]:
    """Add and connect to `targets`; the `relay_key`s that connected. Why each
    other one didn't is logged."""
    for target in targets:
        try:
            await client.add_relay(target.write_url)
        except Exception as e:
            logger.warning(f"{target.url} is not a relay address: {e!r}")
    output = await client.try_connect(CONNECT_TIMEOUT)
    for url, reason in output.failed.items():
        logger.warning(f"couldn't connect to {url}: {reason}")
    return {relay_key(url) for url in output.success}


async def send_event(
    client: Client, event: Event, urls: list[str]
) -> tuple[set[str], dict[str, str]]:
    """The `relay_key`s that took the event, and why each other one didn't: a
    relay's own refusal, or that the send timed out or failed."""
    if not urls:
        return set(), {}
    try:
        output = await asyncio.wait_for(
            client.send_event_to(urls, event), SEND_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return set(), {relay_key(url): "timeout" for url in urls}
    except Exception as e:
        logger.warning(f"send to {urls} failed: {e!r}")
        return set(), {relay_key(url): "not sent" for url in urls}
    return (
        {relay_key(url) for url in output.success},
        {relay_key(url): reason for url, reason in output.failed.items()},
    )


def relay_report(
    targets: list[RelayTarget],
    connected: set[str],
    accepted: set[str],
    refused: dict[str, str],
) -> list[AssistantRelayResult]:
    """One line per target, by its public address: took it, or why not."""
    report: list[AssistantRelayResult] = []
    for target in targets:
        key = relay_key(target.write_url)
        if key in accepted:
            report.append(AssistantRelayResult(url=target.url, ok=True))
            continue
        reason = (
            NOT_REACHED if key not in connected else refused.get(key) or "not sent"
        )
        report.append(AssistantRelayResult(url=target.url, ok=False, message=reason))
    return report


async def send_to(
    event: Event, targets: list[RelayTarget], name: str, author: str
) -> list[AssistantRelayResult]:
    """Connect once to every target, send `event` to those that connected, and
    report per relay."""
    client = Client()
    try:
        connected = await connect_targets(client, targets)
        accepted, refused = await send_event(
            client,
            event,
            [t.write_url for t in targets if relay_key(t.write_url) in connected],
        )
    finally:
        await client.disconnect()
    report = relay_report(targets, connected, accepted, refused)
    for line in report:
        if not line.ok:
            logger.warning(f"{name} for {author} not on {line.url}: {line.message}")
    return report


def reached_some(
    report: list[AssistantRelayResult], name: str
) -> list[AssistantRelayResult]:
    """The report, when some relay took the event; a 502 when none did."""
    if not any(line.ok for line in report):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Your Assistant's {name} didn't reach any relay. Try again in a moment.",
        )
    return report
