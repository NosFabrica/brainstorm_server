"""URL-shortener service.

Short codes live in Postgres, not Redis. They are a record of truth: an evicted
code 404s a public URL permanently and, unlike everything else in that cache,
cannot be recomputed. See .scratch/shorturl/PRD.md D1.

Codes are Crockford base32 — uppercase, and never ``I``, ``L``, ``O`` or ``U``.
Those are the glyphs people mistype when a link is read aloud or retyped, so
resolution folds them back and is case-insensitive. Stored uppercase; callers
may pass any case.

Idempotency comes from a unique constraint on ``(pubkey, relays_fingerprint)``
rather than a second key, so a concurrent double-mint loses the race in the
database instead of orphaning a row.
"""

import hashlib
import secrets
from urllib.parse import urlparse

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.db_models import ShortUrl
from app.repos.short_url_repo import (
    insert_short_url_on_db,
    select_short_url_by_code_on_db,
    select_short_url_by_content_on_db,
)
from app.schemas.schemas import ShortUrlContent

logger = loggr.get_logger(__name__)

# Referenced ONLY by generate_short_code. Nothing else may infer a length from
# it — codes already in the wild must keep resolving if this changes.
SHORT_CODE_LENGTH = 8
MAX_RELAYS = 7

# Crockford base32: the digits and uppercase letters, minus I, L, O and U.
_SHORT_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# What a human might type instead. U is excluded from the alphabet but has no
# digit it could be confused with, so it is not folded.
_CONFUSABLES = str.maketrans({"I": "1", "L": "1", "O": "0"})

_VALID_RELAY_SCHEMES = ("ws", "wss")
_MAX_GENERATION_ATTEMPTS = 5


def generate_short_code() -> str:
    return "".join(
        secrets.choice(_SHORT_CODE_ALPHABET) for _ in range(SHORT_CODE_LENGTH)
    )


def normalize_short_code(raw: str) -> str:
    """Canonical form of a code as typed, scanned, or copied.

    Uppercases and applies Crockford's confusable folding, so a code reached by
    a lowercase link, an uppercase QR payload, or a hand-retyped ``O`` for a
    zero all resolve to the same stored row.
    """
    return (raw or "").strip().upper().translate(_CONFUSABLES)


def _is_valid_relay_url(url: str) -> bool:
    """Format-only check: must be a ws:// or wss:// URL with a host."""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    return parsed.scheme in _VALID_RELAY_SCHEMES and bool(parsed.netloc)


def relays_fingerprint(relays: list[str]) -> str:
    """Stable fingerprint of a relay set, order- and duplicate-insensitive.

    Hostnames are case-insensitive and a trailing slash is not meaningful, so
    we normalize both before hashing to dedupe equivalent relay sets.
    """
    normalized = sorted({r.strip().rstrip("/").lower() for r in relays})
    joined = "\n".join(normalized)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _to_content(row: ShortUrl) -> ShortUrlContent:
    return ShortUrlContent(pubkey=row.pubkey, relays=list(row.relays))


def _validated_pubkey(pubkey: str, relays: list[str]) -> str:
    """Check the whole submission; return the pubkey in its stored form."""
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
    return pubkey


async def create_short_url(
    db: AsyncDBSession, pubkey: str, relays: list[str]
) -> tuple[str, ShortUrlContent]:
    """Return the existing short code for (pubkey, relays) or mint a new one."""
    pubkey = _validated_pubkey(pubkey, relays)
    fingerprint = relays_fingerprint(relays)

    existing = await select_short_url_by_content_on_db(db, pubkey, fingerprint)
    if existing:
        return existing.short_code, _to_content(existing)

    for _ in range(_MAX_GENERATION_ATTEMPTS):
        try:
            # A savepoint so a unique violation can be retried without poisoning
            # the surrounding transaction.
            async with db.begin_nested():
                row = await insert_short_url_on_db(
                    db,
                    short_code=generate_short_code(),
                    pubkey=pubkey,
                    relays_fingerprint=fingerprint,
                    relays=relays,
                )
        except IntegrityError:
            # Either the code collided, or another request minted this exact
            # (pubkey, relay-set) first. The latter is settled, not retried.
            concurrent = await select_short_url_by_content_on_db(
                db, pubkey, fingerprint
            )
            if concurrent:
                return concurrent.short_code, _to_content(concurrent)
            continue
        return row.short_code, _to_content(row)

    logger.error(
        "Failed to generate a unique short code after %d attempts",
        _MAX_GENERATION_ATTEMPTS,
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not generate a unique short code, please retry",
    )


async def get_short_url_content(db: AsyncDBSession, short_code: str) -> ShortUrlContent:
    row = await select_short_url_by_code_on_db(db, normalize_short_code(short_code))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Short url not found",
        )
    return _to_content(row)
