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
from app.services.shorturl_service import create_short_url, get_short_url_content

pytestmark = pytest.mark.integration

PK = "f4d1866e8599563c52ceeedf11c28b8567e465c6e9a91df92add535d57f02ab0"


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
    """AC: 'Mint a code, restart the API, resolve it — it still works'.

    A second ``session()`` is a fresh session and identity map, so resolving
    through it proves the row came back from Postgres, not from memory.
    """

    async def main(session, minted):
        async with session() as db:
            code = (await create_short_url(db, PK, ["wss://relay.damus.io"])).short_code
        minted.append(code)

        async with session() as db:
            content = await get_short_url_content(db, code)

        assert content.pubkey == PK
        assert content.relays == ["wss://relay.damus.io"]

    run(main)


def test_minting_is_idempotent_across_equivalent_relay_sets():
    async def main(session, minted):
        async with session() as db:
            first = (
                await create_short_url(
                    db, PK, ["wss://relay.damus.io", "wss://nos.lol"]
                )
            ).short_code
        minted.append(first)

        async with session() as db:
            again = await create_short_url(
                db, PK, ["wss://NOS.lol/", "wss://relay.damus.io"]
            )
        assert again.short_code == first, "an equivalent relay set must reuse the code"
        assert again.content.relays == ["wss://relay.damus.io", "wss://nos.lol"]

        async with session() as db:
            different = (await create_short_url(db, PK, ["wss://nos.lol"])).short_code
        minted.append(different)
        assert different != first, "a different relay set must mint its own code"

    run(main)


def test_a_code_resolves_however_it_was_typed():
    async def main(session, minted):
        async with session() as db:
            code = (await create_short_url(db, PK, [])).short_code
        minted.append(code)

        folded = code.replace("1", "I").replace("0", "O")
        for variant in (code, code.lower(), code.swapcase(), folded, f"  {code}  "):
            async with session() as db:
                assert (await get_short_url_content(db, variant)).pubkey == PK, variant

    run(main)


def test_an_unknown_code_is_a_404():
    async def main(session, minted):
        async with session() as db:
            with pytest.raises(HTTPException) as excinfo:
                await get_short_url_content(db, "ZZZZZZZZ")
            assert excinfo.value.status_code == 404

    run(main)
