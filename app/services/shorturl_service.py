"""URL-shortener service. Storage rationale: docs/adr/0002-short-links-in-postgres.md."""

import secrets

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.db_models import ShortUrl
from app.repos.short_url_repo import (
    insert_short_url_on_db,
    select_short_url_by_code_on_db,
    select_short_url_by_entity_on_db,
)
from app.schemas.schemas import CreatedShortUrl, ShortUrlEntity
from app.utils.shorturl_entities import EntityKind, build_entity_path

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


def _to_entity(row: ShortUrl) -> ShortUrlEntity:
    relays = list(row.relays)
    return ShortUrlEntity(
        kind=row.kind,
        key=row.entity_key,
        relays=relays,
        path=build_entity_path(row.kind, row.entity_key, relays),
    )


def _to_created(row: ShortUrl) -> CreatedShortUrl:
    return CreatedShortUrl(short_code=row.short_code, **_to_entity(row).model_dump())


async def create_short_url(
    db: AsyncDBSession, kind: EntityKind, entity_key: str, relays: list[str]
) -> CreatedShortUrl:
    """Return the existing code for the entity, stored relays untouched, or mint one."""
    existing = await select_short_url_by_entity_on_db(db, kind, entity_key)
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
                    kind=kind,
                    entity_key=entity_key,
                    relays=relays,
                )
        except IntegrityError:
            # Either the code collided, or another request minted this entity
            # first. The latter is settled, not retried.
            concurrent = await select_short_url_by_entity_on_db(db, kind, entity_key)
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


async def get_short_url_entity(db: AsyncDBSession, short_code: str) -> ShortUrlEntity:
    row = await select_short_url_by_code_on_db(db, normalize_short_code(short_code))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Short url not found",
        )
    return _to_entity(row)
