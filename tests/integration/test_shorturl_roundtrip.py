"""Short links against real Postgres: mint, resolve, and survive a reconnect.

The fast suite mocks the repo, so this is the only place the unique constraints,
the JSONB column and the savepoint retry meet an actual database.

Requires the local stack (`docker compose up -d`) and migrations at head.
Deselect with `-m 'not integration'`.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db_models import ShortUrl
from app.services.shorturl_service import create_short_url, get_short_url_entity

pytestmark = pytest.mark.integration

PK = "f4d1866e8599563c52ceeedf11c28b8567e465c6e9a91df92add535d57f02ab0"
EVENT_ID = "5c83da77af1dec6d7289834998ad7aafbd9e2191396d75ec3cc27f5a77226f36"


def run(main):
    """Run `async main(session, minted)` on a fresh engine, as tagging_harness does.

    `session()` opens a new session and commits on exit; codes appended to
    `minted` are deleted afterwards.
    """

    async def _go():
        engine = create_async_engine(settings.db_url, future=True)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False, future=True)

        @asynccontextmanager
        async def session():
            async with factory() as db:
                yield db
                await db.commit()

        minted: list[str] = []
        try:
            return await main(session, minted)
        finally:
            if minted:
                async with session() as db:
                    await db.execute(
                        delete(ShortUrl).where(ShortUrl.short_code.in_(minted))
                    )
            await engine.dispose()

    return asyncio.run(_go())


def test_a_link_survives_a_new_connection():
    """A second ``session()`` is a fresh session and identity map, so resolving
    through it proves the row came back from Postgres, not from memory.
    """

    async def main(session, minted):
        async with session() as db:
            code = (
                await create_short_url(db, "profile", PK, ["wss://relay.damus.io"])
            ).short_code
        minted.append(code)

        async with session() as db:
            entity = await get_short_url_entity(db, code)

        assert (entity.kind, entity.key) == ("profile", PK)
        assert entity.relays == ["wss://relay.damus.io"]
        assert entity.path.startswith("/p/nprofile1")

    run(main)


def test_the_same_entity_keeps_its_code_and_first_relays():
    async def main(session, minted):
        async with session() as db:
            first = await create_short_url(db, "event", EVENT_ID, ["wss://nos.lol"])
        minted.append(first.short_code)

        async with session() as db:
            again = await create_short_url(db, "event", EVENT_ID, [])
        assert again.short_code == first.short_code
        assert again.relays == ["wss://nos.lol"], "first mint's relays win"

        async with session() as db:
            other = await create_short_url(db, "hashtag", "nostr", [])
        minted.append(other.short_code)
        assert other.short_code != first.short_code

    run(main)


def test_a_concurrent_mint_settles_on_the_winner(monkeypatch):
    """The loser's dedup read misses, its insert hits the unique constraint
    inside a savepoint, and it returns the winner's row."""
    from app.services import shorturl_service as svc

    real_lookup = svc.select_short_url_by_entity_on_db
    misses = iter([True])

    async def stale_first_lookup(db, kind, key):
        if next(misses, False):
            return None
        return await real_lookup(db, kind, key)

    async def main(session, minted):
        async with session() as db:
            winner = await create_short_url(db, "tag", f"{PK}:slug", [])
        minted.append(winner.short_code)

        monkeypatch.setattr(svc, "select_short_url_by_entity_on_db", stale_first_lookup)
        async with session() as db:
            loser = await create_short_url(db, "tag", f"{PK}:slug", ["wss://x.example"])
        assert loser.short_code == winner.short_code
        assert loser.relays == []

    run(main)


def test_a_code_resolves_however_it_was_typed():
    async def main(session, minted):
        async with session() as db:
            code = (await create_short_url(db, "profile", PK, [])).short_code
        minted.append(code)

        folded = code.replace("1", "I").replace("0", "O")
        for variant in (code, code.lower(), code.swapcase(), folded, f"  {code}  "):
            async with session() as db:
                assert (await get_short_url_entity(db, variant)).key == PK, variant

    run(main)


def test_an_unknown_code_is_a_404():
    async def main(session, minted):
        async with session() as db:
            with pytest.raises(HTTPException) as excinfo:
                await get_short_url_entity(db, "ZZZZZZZZ")
            assert excinfo.value.status_code == 404

    run(main)
