"""URL-shortener service. Storage rationale: docs/adr/0002-short-links-in-postgres.md."""

import hashlib
import secrets

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
from app.schemas.schemas import CreatedShortUrl, ShortUrlContent

logger = loggr.get_logger(__name__)

# Generation only — nothing may infer a length from it.
SHORT_CODE_LENGTH = 8

# Crockford base32: the digits and uppercase letters, minus I, L, O and U.
_SHORT_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# U has no digit lookalike, so it isn't folded.
_CONFUSABLES = str.maketrans({"I": "1", "L": "1", "O": "0"})

_MAX_GENERATION_ATTEMPTS = 5


def generate_short_code() -> str:
    return "".join(
        secrets.choice(_SHORT_CODE_ALPHABET) for _ in range(SHORT_CODE_LENGTH)
    )


def normalize_short_code(raw: str) -> str:
    """Uppercase with Crockford's confusables folded, however the code was typed."""
    return raw.strip().upper().translate(_CONFUSABLES)


def relays_fingerprint(relays: list[str]) -> str:
    """Order-, duplicate-, case- and trailing-slash-insensitive hash of a relay set."""
    normalized = sorted({r.strip().rstrip("/").lower() for r in relays})
    joined = "\n".join(normalized)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _to_content(row: ShortUrl) -> ShortUrlContent:
    return ShortUrlContent(pubkey=row.pubkey, relays=list(row.relays))


def _to_created(row: ShortUrl) -> CreatedShortUrl:
    return CreatedShortUrl(short_code=row.short_code, content=_to_content(row))


async def create_short_url(
    db: AsyncDBSession, pubkey: str, relays: list[str]
) -> CreatedShortUrl:
    """Return the existing short code for (pubkey, relays) or mint a new one."""
    fingerprint = relays_fingerprint(relays)

    existing = await select_short_url_by_content_on_db(db, pubkey, fingerprint)
    if existing:
        return _to_created(existing)

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
                return _to_created(concurrent)
            continue
        return _to_created(row)

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
