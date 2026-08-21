"""URL-shortener service.

Stores short codes in Redis, keyed two ways:

* ``shorturl:content:<code>``  -> JSON ``{"pubkey": ..., "relays": [...]}``.
  This is the canonical record a GET resolves.
* ``shorturl:fp:<pubkey>:<fingerprint>`` -> short code. A reverse index that
  gives O(1) dedup so the same (pubkey, relay-set) pair never gets two
  different short codes. The fingerprint is the hash of the alphabetically
  sorted, normalized relay set (see ``_relays_fingerprint``).

Expiry is opt-in via ``settings.shorturl_ttl_seconds`` (None = never expire,
the current default). Both the content key and its index key are written with
the same TTL, so they auto-delete together — no background sweep needed. The
dangling-entry guard in ``create_short_url`` still covers the rare case where
the content is gone but the index lingers (e.g. eviction skew).
"""

import hashlib
import json
import secrets
import string
from urllib.parse import urlparse

from fastapi import HTTPException, status

from app.core.config import settings
from app.core.loggr import loggr
from app.core.redis_db import redis_client
from app.schemas.schemas import ShortUrlContent

logger = loggr.get_logger(__name__)

SHORT_CODE_LENGTH = 12
MAX_RELAYS = 7
_SHORT_CODE_ALPHABET = string.ascii_letters + string.digits
_VALID_RELAY_SCHEMES = ("ws", "wss")
_MAX_GENERATION_ATTEMPTS = 5

_CONTENT_KEY_PREFIX = "shorturl:content:"
_FINGERPRINT_KEY_PREFIX = "shorturl:fp:"


def _content_key(short_code: str) -> str:
    return f"{_CONTENT_KEY_PREFIX}{short_code}"


def _fingerprint_key(pubkey: str, fingerprint: str) -> str:
    return f"{_FINGERPRINT_KEY_PREFIX}{pubkey}:{fingerprint}"


def _is_valid_relay_url(url: str) -> bool:
    """Format-only check: must be a ws:// or wss:// URL with a host."""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    return parsed.scheme in _VALID_RELAY_SCHEMES and bool(parsed.netloc)


def _relays_fingerprint(relays: list[str]) -> str:
    """Stable fingerprint of a relay set, order- and duplicate-insensitive.

    Hostnames are case-insensitive and a trailing slash is not meaningful, so
    we normalize both before hashing to dedupe equivalent relay sets.
    """
    normalized = sorted({r.strip().rstrip("/").lower() for r in relays})
    joined = "\n".join(normalized)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _generate_short_code() -> str:
    return "".join(
        secrets.choice(_SHORT_CODE_ALPHABET) for _ in range(SHORT_CODE_LENGTH)
    )


async def _store_new_short_code(content_json: str) -> str:
    """Generate a unique code and claim it atomically via SET NX."""
    ttl = settings.shorturl_ttl_seconds
    for _ in range(_MAX_GENERATION_ATTEMPTS):
        short_code = _generate_short_code()
        created = await redis_client.set(
            _content_key(short_code), content_json, nx=True, ex=ttl
        )
        if created:
            return short_code

    logger.error(
        "Failed to generate a unique short code after %d attempts",
        _MAX_GENERATION_ATTEMPTS,
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not generate a unique short code, please retry",
    )


async def create_short_url(
    pubkey: str, relays: list[str]
) -> tuple[str, ShortUrlContent]:
    """Return an existing short code for (pubkey, relays) or create a new one.

    Returns ``(short_code, content)``.
    """
    pubkey = pubkey.strip()
    if not pubkey:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="pubkey is required",
        )
    # An empty relay list is a valid submission. Any relays that ARE provided
    # must be well-formed, and there can be at most MAX_RELAYS of them.
    if len(relays) > MAX_RELAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_RELAYS} relays are allowed",
        )

    invalid = [r for r in relays if not _is_valid_relay_url(r)]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid relay url(s): {invalid}",
        )

    fingerprint = _relays_fingerprint(relays)
    index_key = _fingerprint_key(pubkey, fingerprint)

    content = ShortUrlContent(pubkey=pubkey, relays=relays)

    existing_code = await redis_client.get(index_key)
    if existing_code:
        if await redis_client.exists(_content_key(existing_code)):
            return existing_code, content
        # Content expired/evicted but the index entry lingered; drop it and
        # fall through to create a fresh code.
        await redis_client.delete(index_key)

    short_code = await _store_new_short_code(content.model_dump_json())
    await redis_client.set(index_key, short_code, ex=settings.shorturl_ttl_seconds)

    return short_code, content


async def get_short_url_content(short_code: str) -> ShortUrlContent:
    raw = await redis_client.get(_content_key(short_code))
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Short url not found",
        )
    return ShortUrlContent(**json.loads(raw))
