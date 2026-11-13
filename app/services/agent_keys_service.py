"""The Assistant's AI keys: `GET`/`POST /user/assistantAgentKeys`.

The Assistant holds its own NIP-FA list (kind 12000, signed by the Assistant,
private tags encrypted to itself), which the Observer print worker reads
through `/internal/observerRunner/decrypt` to print for the owner. The owner
gives it a key from Brainstorm-UI's AI key screen; nothing else writes it.

Both routes find the Assistant from the login token's pubkey alone
(`owner_assistant`), and the GET encrypts the list to the owner on record, so a
mix-up anywhere leaks only ciphertext. The relay event is the storage: nothing
is kept in Postgres.

NIP-FA's editing rules: read the newest version at the Assistant's outbox
(`assistant_relay_list()`), with more than half of the relays answering, since
one that never saw the event answers "none" as fast as one that holds it; write
`created_at` = max(now, previous + 1) to the same relays. The read returns once
a majority has answered, plus a short grace (`relay_session`), and the POST
sends on the sockets it read with. The UI does the read-modify-write, and
`previousId` / `previousCreatedAt` (the GET's `id` and `createdAt`) are how the
server knows it edited the version that is newest now (409 otherwise). One
edit per Assistant at a time across every replica: a Redis lock
(`SET NX EX`), released when the edit ends.

Decrypted keys are never logged, never put in an error, and never returned but
as the GET's ciphertext to the owner. Errors and logs name a tag by position.
"""
from __future__ import annotations

import secrets
import time
from typing import Any

from fastapi import HTTPException, status
from nostr_sdk import Keys  # type: ignore
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.core.redis_db import redis_client
from app.db_models import BrainstormNsec
from app.repos.brainstorm_nsec import select_brainstorm_nsec_by_pubkey_on_db
from app.schemas.schemas import AssistantAgentKeys, PublishedAssistantAgentKeys
from app.services.agent_keys_build import (
    AGENT_KEYS_KIND,
    AgentKeysRejected,
    check_new_list,
    has_quorum,
    is_expected_version,
    newest,
    next_created_at,
    parse_tag_array,
    serialize,
)
from app.services.assistant_profile_service import assistant_relay_list
from app.services.assistant_publish import is_own, sign_checked
from app.services.duty_menu_build import RelayTarget, publish_targets
from app.services.relay_session import RelayGroup
from app.utils import nip44

logger = loggr.get_logger(__name__)

NAME = "AI keys"
NO_ASSISTANT = "no-assistant"

# One edit at a time per Assistant, across replicas: two presses racing past the
# same previous version would each sign one, and the second would drop the
# first one's change. Longer than an edit takes (a 5 s read, a 10 s send, the
# closes), short enough that a crashed holder blocks the owner only briefly.
LOCK_TTL_SECONDS = 30
_LOCK_PREFIX = "lock:assistant_agent_keys:"
# Deletes the lock only while it is still ours: an edit that outlived the TTL
# must not release the next edit's lock.
_RELEASE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


async def owner_assistant(
    db: AsyncDBSession, owner: str
) -> tuple[BrainstormNsec, Keys]:
    """`owner`'s Assistant: its row and key. 404 + `X-Reason: no-assistant`
    when the owner has none. Never creates one."""
    try:
        row = await select_brainstorm_nsec_by_pubkey_on_db(db, owner)
    except HTTPException as e:
        if e.status_code != status.HTTP_404_NOT_FOUND:
            raise
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You don't have a Brainstorm Assistant yet.",
            headers={"X-Reason": NO_ASSISTANT},
        ) from None
    return row, Keys.parse(secret_key=row.nsec)


def _outbox() -> list[RelayTarget]:
    return publish_targets(assistant_relay_list())


def _unreadable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Couldn't read your Assistant's AI keys from enough relays. Try again in a moment.",
    )


async def _read_newest_list(
    relays: RelayGroup, assistant_pubkey: str
) -> dict[str, Any] | None:
    """The newest kind 12000 of the Assistant's that the outbox holds, read by
    majority (NIP-FA). None when there is none. 502 when half or fewer answer."""
    held = await relays.read_quorum(
        {"kinds": [AGENT_KEYS_KIND], "authors": [assistant_pubkey], "limit": 1},
        lambda e: is_own(e, assistant_pubkey, AGENT_KEYS_KIND),
        NAME,
    )
    if not has_quorum(len(held), len(relays.targets)):
        logger.warning(
            f"{NAME} for {assistant_pubkey}: {len(held)} of "
            f"{len(relays.targets)} relays answered"
        )
        raise _unreadable()
    return newest([e for e in held.values() if e is not None])


async def _lock(who: str) -> str:
    """Take the Assistant's edit lock; the token that releases it. 409 when
    another edit holds it, 503 when Redis can't be asked."""
    token = secrets.token_hex(16)
    try:
        taken = await redis_client.set(
            _LOCK_PREFIX + who, token, nx=True, ex=LOCK_TTL_SECONDS
        )
    except RedisError as e:
        logger.error(f"{NAME} for {who}: the edit lock is unavailable: {e!r}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Your Assistant's AI keys can't be changed right now. Try again in a moment.",
        ) from None
    if not taken:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Your Assistant's AI keys are being changed already. Try again in a moment.",
        )
    return token


async def _unlock(who: str, token: str) -> None:
    try:
        await redis_client.eval(_RELEASE, 1, _LOCK_PREFIX + who, token)  # type: ignore[misc]
    except RedisError as e:
        # It expires on its own within LOCK_TTL_SECONDS.
        logger.warning(f"{NAME} for {who}: the edit lock wasn't released: {e!r}")


def _decrypt_own_list(keys: Keys, event: dict[str, Any]) -> list[list[str]]:
    """The private tags of the Assistant's own list. 502 when they don't decrypt
    to a tag array: NIP-FA forbids writing over a list that can't be read."""
    who = keys.public_key().to_hex()
    try:
        return parse_tag_array(nip44.decrypt(keys, who, event.get("content") or ""))
    except (nip44.Nip44Error, AgentKeysRejected) as e:
        # The reason is a fixed sentence; the content is not logged.
        logger.error(f"{NAME} for {who}: the newest version is unreadable ({e})")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Your Assistant's newest AI key list can't be read.",
        ) from None


async def read_assistant_agent_keys(
    db: AsyncDBSession, owner: str
) -> AssistantAgentKeys:
    row, keys = await owner_assistant(db, owner)
    who = keys.public_key().to_hex()
    async with RelayGroup(_outbox()) as relays:
        event = await _read_newest_list(relays, who)
    if event is None:
        return AssistantAgentKeys(assistant=who, ciphertext=None, created_at=None)
    tags = _decrypt_own_list(keys, event)
    # To the owner on record, never to whoever asked.
    ciphertext = nip44.encrypt(keys, row.pubkey, serialize(tags))
    return AssistantAgentKeys(
        assistant=who,
        ciphertext=ciphertext,
        created_at=int(event["created_at"]),
        id=event["id"],
    )


async def publish_assistant_agent_keys(
    db: AsyncDBSession,
    owner: str,
    ciphertext: str,
    previous_created_at: int | None,
    previous_id: str | None = None,
) -> PublishedAssistantAgentKeys:
    row, keys = await owner_assistant(db, owner)
    who = keys.public_key().to_hex()

    try:
        plaintext = nip44.decrypt(keys, row.pubkey, ciphertext)
    except nip44.Nip44Error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The new list isn't encrypted from you to your Assistant.",
        ) from None
    try:
        tags = check_new_list(plaintext)
    except AgentKeysRejected as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from None
    del plaintext

    token = await _lock(who)
    try:
        async with RelayGroup(_outbox()) as relays:
            current = await _read_newest_list(relays, who)
            if not is_expected_version(current, previous_id, previous_created_at):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Your Assistant's AI keys changed since you read them. "
                        "Reload and try again."
                    ),
                )
            current_at = int(current["created_at"]) if current is not None else None
            if current is not None:
                # Not overwritten unless it can be read, as NIP-FA requires.
                _decrypt_own_list(keys, current)

            content = nip44.encrypt(keys, who, serialize(tags))
            created_at = next_created_at(int(time.time()), current_at)
            _, signed = sign_checked(
                keys,
                AGENT_KEYS_KIND,
                content,
                [],
                created_at,
                lambda s: s.get("pubkey") == who
                and s.get("kind") == AGENT_KEYS_KIND
                and s.get("content") == content,
                NAME,
            )
            report = await relays.send(signed, NAME, who)
    finally:
        await _unlock(who, token)

    accepted = sum(1 for line in report if line.ok)
    if accepted * 2 < len(report):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Only {accepted} of {len(report)} relays took your Assistant's "
                "new AI key list. Reload and try again."
            ),
        )
    logger.info(f"{NAME} for {who}: published {len(tags)} tags at {created_at}")
    return PublishedAssistantAgentKeys(
        created_at=created_at, id=signed["id"], relays=report
    )
