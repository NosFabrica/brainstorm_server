import asyncio
import json

from fastapi import HTTPException, status
from neo4j.exceptions import TransientError
from nostr_sdk import Event

from app.core.loggr import loggr
from app.message_queue_tasks.process_strfry_event import process_event_kind_3
from app.neo4j_db.driver import driver as neo4j_driver

logger = loggr.get_logger(__name__)

KIND_FOLLOW_LIST = 3

# A synchronous onboarding write failing on a transient Neo4j lock is more
# user-visible than the background consumer (which just retries on the next
# message), so retry transient/deadlock errors here. The write is an idempotent
# MERGE, so re-applying after a partial failure is safe.
_MAX_WRITE_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 0.05


async def ingest_follow_list(caller_pubkey: str, signed_event: dict) -> int:
    """Validate and write an onboarding follow list into Neo4j synchronously.

    Reuses the normal kind-3 ingest handler so the FOLLOWS edges and the
    ``followed_by:`` Redis reverse-sets land before the request returns.
    """
    try:
        event = Event.from_json(json.dumps(signed_event))
    except Exception as exc:
        # The body already passed NostrEvent schema validation, so this is a
        # residual nostr_sdk gripe (e.g. canonical-serialization quirk). Surface
        # it as a clean 400 rather than letting it become an unhandled 500.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed Nostr event",
        ) from exc

    if event.kind().as_u16() != KIND_FOLLOW_LIST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Event must be a kind-3 follow list",
        )
    if not event.verify_signature():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid event signature",
        )
    if event.author().to_hex() != caller_pubkey:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Event author does not match the authenticated user",
        )

    await _write_follows_with_retry(signed_event)

    return sum(1 for tag in signed_event.get("tags", []) if tag and tag[0] == "p")


async def _write_follows_with_retry(signed_event: dict) -> None:
    for attempt in range(_MAX_WRITE_ATTEMPTS):
        try:
            async with neo4j_driver.session() as session:
                await process_event_kind_3(session, signed_event)
            return
        except TransientError:
            if attempt == _MAX_WRITE_ATTEMPTS - 1:
                raise
            logger.warning(
                "Transient Neo4j error writing onboarding follow list; "
                "retrying (attempt %s/%s)",
                attempt + 1,
                _MAX_WRITE_ATTEMPTS,
            )
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
