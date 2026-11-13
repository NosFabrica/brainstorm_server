"""The Observer print worker's side of the Assistant: `/internal/observerRunner/*`.

The worker (a JVM container beside this server, on the internal network) holds
no nsec. It asks here to list the Assistants, to decrypt as one, and to sign as
one. The guarantee this server gives is the per-owner binding: every call names
an owner, and only that owner's Assistant decrypts or signs, for that owner.
The server checks every event it decrypts; the worker checks schedules too,
with Quartz's runner helper, before printing (defence in depth).

Strict allow-lists:
- `decrypt` takes a whole signed event, never a bare ciphertext, so it is no
  general decryption oracle for the owner/Assistant pair. After the id and
  signature verify, it opens only a schedule (34817 by the owner, with exactly
  one `p`, the owner's Assistant; the owner is the peer) or the Assistant's own
  AI keys (12000 by the Assistant; itself is the peer), and the AI keys only
  beside the owner's schedule naming that Assistant (`schedule`, checked as a
  34817 is, not decrypted): the keys open only for a print the owner asked
  this Assistant for. Anything else is a 403.
- `sign` signs two kinds: the Assistant's Observer site (35128, exactly one
  `d` = `observer`, exactly one `p`, the owner) and a Blossom upload
  authorization (24242, exactly one `t` = `upload`, an `expiration` in the next
  10 minutes, no `p` naming the Assistant). Every other kind or shape is a 403.

Logs carry owner, kind and outcome; never a plaintext or a ciphertext.
"""
from __future__ import annotations

import re
import time
from typing import Any

from fastapi import HTTPException, status
from nostr_sdk import Event  # type: ignore
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.repos.brainstorm_nsec import select_owner_assistant_pairs_on_db
from app.schemas.schemas import (
    ObserverRunnerAssistant,
    ObserverRunnerAssistants,
    ObserverRunnerPlaintext,
)
from app.services.agent_keys_build import AGENT_KEYS_KIND
from app.services.agent_keys_service import owner_assistant
from app.services.assistant_publish import sign_checked
from app.utils import nip44
from app.utils.client_tag import CLIENT_TAG
from app.utils.nostr import event_tags
from app.utils.signed_event import SignedEventRejected, verify_signed_event

logger = loggr.get_logger(__name__)

OBSERVER_SITE_KIND = 35128
OBSERVER_SITE_D = "observer"
BLOSSOM_AUTH_KIND = 24242
BLOSSOM_UPLOAD = "upload"
# How far ahead a Blossom authorization may expire, and an event be dated.
MAX_AHEAD_SECONDS = 600

SIGNABLE_KINDS = (OBSERVER_SITE_KIND, BLOSSOM_AUTH_KIND)


async def list_assistants(
    db: AsyncDBSession, after: str | None, limit: int
) -> ObserverRunnerAssistants:
    """Every owner with an Assistant, by owner pubkey, after `after`."""
    rows = await select_owner_assistant_pairs_on_db(db, after=after, limit=limit + 1)
    page = rows[:limit]
    return ObserverRunnerAssistants(
        items=[ObserverRunnerAssistant(owner=o, assistant=a) for o, a in page],
        next=page[-1][0] if len(rows) > limit else None,
    )


SCHEDULE_KIND = 34817
DECRYPTABLE_KINDS = (SCHEDULE_KIND, AGENT_KEYS_KIND)


def _refuse_decrypt(owner: str, kind: Any, why: str) -> HTTPException:
    logger.info(
        f"observerRunner decrypt owner={owner} kind={kind} outcome=refused ({why})"
    )
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the owner's schedule or the Assistant's own AI keys are decrypted.",
    )


def _schedule_refusal(event: Event, owner: str, assistant: str) -> str | None:
    """Why a verified 34817 isn't `owner`'s schedule naming `assistant`, or None."""
    if event.author().to_hex() != owner:
        return "author"
    p_tags = [t for t in event_tags(event) if t and t[0] == "p"]
    if len(p_tags) != 1:
        return "p count"
    if p_tags[0][1:2] != [assistant]:
        return "p"
    return None


async def decrypt_as_assistant(
    db: AsyncDBSession,
    owner: str,
    raw_event: dict[str, Any],
    raw_schedule: dict[str, Any] | None = None,
) -> ObserverRunnerPlaintext:
    """The content of a schedule or of the Assistant's own list, decrypted by
    `owner`'s Assistant. 403 for any other event, before anything is decrypted.
    The list (12000) opens only beside `raw_schedule`, the owner's verified
    34817 naming that Assistant; for a 34817, `raw_schedule` is ignored."""
    given = raw_event.get("kind")
    # Logged, so only ever one of ours: the event is the caller's.
    kind = given if type(given) is int and given in DECRYPTABLE_KINDS else None
    if kind is None:
        raise _refuse_decrypt(owner, "other", "kind")
    if kind == AGENT_KEYS_KIND and raw_schedule is None:
        raise _refuse_decrypt(owner, kind, "no schedule")
    try:
        event = verify_signed_event(raw_event, kind=kind)
    except SignedEventRejected as e:
        raise _refuse_decrypt(owner, kind, e.reason.value) from None

    row, keys = await owner_assistant(db, owner)
    assistant = keys.public_key().to_hex()
    if kind == SCHEDULE_KIND:
        why = _schedule_refusal(event, row.pubkey, assistant)
        if why is not None:
            raise _refuse_decrypt(owner, kind, why)
        peer = row.pubkey
    else:
        if event.author().to_hex() != assistant:
            raise _refuse_decrypt(owner, kind, "author")
        try:
            schedule = verify_signed_event(raw_schedule, kind=SCHEDULE_KIND)
        except SignedEventRejected as e:
            raise _refuse_decrypt(owner, kind, f"schedule {e.reason.value}") from None
        why = _schedule_refusal(schedule, row.pubkey, assistant)
        if why is not None:
            raise _refuse_decrypt(owner, kind, f"schedule {why}")
        peer = assistant

    try:
        plaintext = nip44.decrypt(keys, peer, event.content())
    except nip44.Nip44Error:
        logger.info(f"observerRunner decrypt owner={owner} kind={kind} outcome=failed")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The event's content doesn't decrypt.",
        ) from None
    logger.info(f"observerRunner decrypt owner={owner} kind={kind} outcome=ok")
    return ObserverRunnerPlaintext(plaintext=plaintext)


def _values(tags: list[list[str]], name: str) -> list[str]:
    return [t[1] if len(t) > 1 else "" for t in tags if t[0] == name]


# An `expiration` as NIP-40 writes it: ASCII digits only. `str.isdigit` also
# takes "²" (which `int` then refuses) and other scripts' digits.
_SECONDS = re.compile(r"[0-9]{1,10}")


def signing_refusal(
    owner: str,
    assistant: str,
    kind: int,
    created_at: int,
    tags: list[list[str]],
    now: int,
) -> str | None:
    """Why the worker's event may not be signed by `owner`'s Assistant
    (`assistant`), or None when it may."""
    if kind not in SIGNABLE_KINDS:
        return "This kind isn't signed for the Observer runner."
    if created_at > now + MAX_AHEAD_SECONDS:
        return "created_at is more than 10 minutes ahead."
    if kind == OBSERVER_SITE_KIND:
        if _values(tags, "d") != [OBSERVER_SITE_D]:
            return "An Observer site needs exactly one d tag, observer."
        if _values(tags, "p") != [owner]:
            return "An Observer site needs exactly one p tag, the owner."
        return None
    # BLOSSOM_AUTH_KIND
    if _values(tags, "t") != [BLOSSOM_UPLOAD]:
        return "A Blossom authorization is signed for upload only."
    if assistant in _values(tags, "p"):
        # nostr-sdk would drop it, and the event then fails its own check.
        return "A Blossom authorization can't name the Assistant in a p tag."
    expirations = _values(tags, "expiration")
    if len(expirations) != 1 or not _SECONDS.fullmatch(expirations[0]):
        return "A Blossom authorization needs one expiration."
    expiration = int(expirations[0])
    if expiration <= now or expiration > now + MAX_AHEAD_SECONDS:
        return "A Blossom authorization must expire within the next 10 minutes."
    return None


async def sign_as_assistant(
    db: AsyncDBSession,
    owner: str,
    kind: int,
    created_at: int,
    tags: list[list[str]],
    content: str,
) -> dict[str, Any]:
    """The worker's event, signed by `owner`'s Assistant, with our client tag
    in place of any it carried."""
    _, keys = await owner_assistant(db, owner)
    who = keys.public_key().to_hex()
    refusal = signing_refusal(owner, who, kind, created_at, tags, int(time.time()))
    if refusal is not None:
        logger.info(f"observerRunner sign owner={owner} kind={kind} outcome=refused")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)
    own_tags = [t for t in tags if t[0] != CLIENT_TAG[0]]
    _, signed = sign_checked(
        keys,
        kind,
        content,
        own_tags,
        created_at,
        lambda s: s.get("pubkey") == who
        and s.get("kind") == kind
        and s.get("content") == content,
        "Observer runner event",
    )
    logger.info(f"observerRunner sign owner={owner} kind={kind} outcome=signed")
    return signed
