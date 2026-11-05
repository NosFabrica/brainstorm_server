import asyncio

from fastapi import HTTPException, status
from neo4j.exceptions import TransientError

from app.core.loggr import loggr
from app.message_queue_tasks.process_strfry_event import process_event_kind_3
from app.neo4j_db.driver import driver as neo4j_driver
from app.utils.nostr import event_tags
from app.utils.signed_event import Rejection, SignedEventRejected, verify_signed_event

logger = loggr.get_logger(__name__)

KIND_FOLLOW_LIST = 3

# A synchronous onboarding write failing on a transient Neo4j lock is more
# user-visible than the background consumer (which just retries on the next
# message), so retry transient/deadlock errors here. The write is an idempotent
# MERGE, so re-applying after a partial failure is safe.
_MAX_WRITE_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 0.05


_REJECTIONS = {
    Rejection.MALFORMED: (status.HTTP_400_BAD_REQUEST, "Malformed Nostr event"),
    Rejection.WRONG_KIND: (
        status.HTTP_400_BAD_REQUEST,
        "Event must be a kind-3 follow list",
    ),
    Rejection.BAD_SIGNATURE: (
        status.HTTP_401_UNAUTHORIZED,
        "Invalid event signature",
    ),
    Rejection.WRONG_AUTHOR: (
        status.HTTP_403_FORBIDDEN,
        "Event author does not match the authenticated user",
    ),
}


async def ingest_follow_list(caller_pubkey: str, signed_event: dict) -> int:
    """Validate and write an onboarding follow list into Neo4j synchronously.

    Reuses the normal kind-3 ingest handler so the FOLLOWS edges and the
    ``followed_by:`` Redis reverse-sets land before the request returns.
    """
    try:
        event = verify_signed_event(
            signed_event, kind=KIND_FOLLOW_LIST, author=caller_pubkey
        )
    except SignedEventRejected as exc:
        status_code, detail = _REJECTIONS[exc.reason]
        raise HTTPException(status_code=status_code, detail=detail) from exc

    follow_count = sum(1 for tag in event_tags(event) if tag and tag[0] == "p")
    # Onboarding is additive-only: this endpoint seeds follows ahead of relay
    # sync. The reused kind-3 handler treats an empty list as "delete all
    # follows" (replaceable-event semantics) — never the intent here, and a
    # client submitting empty by accident would silently wipe the graph.
    # Require at least one follow; an empty kind-3 the user later publishes on
    # their own relay is the relay consumer's concern, not ours.
    if follow_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Follow list must contain at least one follow",
        )

    await _write_follows_with_retry(signed_event)

    return follow_count


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
