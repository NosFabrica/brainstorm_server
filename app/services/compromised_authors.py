"""The marked compromised keys, cached in-process for the strfry ingest, which
checks every relationship event's author against them. Its own module so the
ingest needn't import `compromised_service` (which imports the ingest's sets).
"""

import time

from app.core.database import db_session
from app.core.loggr import loggr
from app.repos.compromised_pubkey_repo import select_compromised_pubkeys_on_db

logger = loggr.get_logger(__name__)

# How stale another replica's view may be after a key is marked; the marking
# process forgets its own cache at once, and the zeroing cuts the key's edges
# again, so an event that slipped through the window is undone.
CACHE_SECONDS = 30.0

_cache: tuple[float, frozenset[str]] = (0.0, frozenset())


async def compromised_authors() -> frozenset[str]:
    """The marked keys. A failed read keeps the last known set: ingest must not
    stop because Postgres blinked."""
    global _cache
    now = time.monotonic()
    if now < _cache[0]:
        return _cache[1]
    try:
        async with db_session() as db:
            keys = frozenset(await select_compromised_pubkeys_on_db(db))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"compromised authors: read failed, keeping the last set: {e!r}")
        keys = _cache[1]
    _cache = (now + CACHE_SECONDS, keys)
    return keys


def forget_compromised_authors() -> None:
    """Re-read on the next event (after a key is marked or unmarked here)."""
    global _cache
    _cache = (0.0, _cache[1])
