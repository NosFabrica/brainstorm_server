"""Short links against real Postgres: mint, resolve, and survive a reconnect.

The fast suite mocks the repo, so this is the only place the unique constraints,
the JSONB column and the savepoint retry meet an actual database.

Each test runs in exactly one event loop and disposes the engine on the way out:
the engine is module-level and pools connections, so a second ``asyncio.run``
would inherit connections bound to a loop that has already closed.

Requires the local stack (`docker compose up -d`) and migrations at head.
Deselect with `-m 'not integration'`.

Issue: .scratch/shorturl/issues/02-durable-storage.md
"""

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from app.core.database import db_session, engine
from app.db_models import ShortUrl
from app.services.shorturl_service import create_short_url, get_short_url_content

pytestmark = pytest.mark.integration

PK = "f4d1866e8599563c52ceeedf11c28b8567e465c6e9a91df92add535d57f02ab0"


def run(main):
    """Drive one async body, then release pooled connections in the same loop."""

    async def _wrapped():
        minted: list[str] = []
        try:
            return await main(minted)
        finally:
            if minted:
                async with db_session() as db:
                    await db.execute(
                        delete(ShortUrl).where(ShortUrl.short_code.in_(minted))
                    )
            await engine.dispose()

    return asyncio.run(_wrapped())


def test_a_link_survives_a_new_connection():
    """AC: 'Mint a code, restart the API, resolve it — it still works'.

    A second ``db_session()`` is a fresh session and identity map, so resolving
    through it proves the row came back from Postgres, not from memory.
    """

    async def main(minted):
        async with db_session() as db:
            code, _ = await create_short_url(db, PK, ["wss://relay.damus.io"])
        minted.append(code)

        async with db_session() as db:
            content = await get_short_url_content(db, code)

        assert content.pubkey == PK
        assert content.relays == ["wss://relay.damus.io"]

    run(main)


def test_minting_is_idempotent_across_equivalent_relay_sets():
    async def main(minted):
        async with db_session() as db:
            first, _ = await create_short_url(
                db, PK, ["wss://relay.damus.io", "wss://nos.lol"]
            )
        minted.append(first)

        async with db_session() as db:
            again, content = await create_short_url(
                db, PK, ["wss://NOS.lol/", "wss://relay.damus.io"]
            )
        assert again == first, "an equivalent relay set must reuse the code"
        assert content.relays == ["wss://relay.damus.io", "wss://nos.lol"]

        async with db_session() as db:
            different, _ = await create_short_url(db, PK, ["wss://nos.lol"])
        minted.append(different)
        assert different != first, "a different relay set must mint its own code"

    run(main)


def test_a_code_resolves_however_it_was_typed():
    async def main(minted):
        async with db_session() as db:
            code, _ = await create_short_url(db, PK, [])
        minted.append(code)

        folded = code.replace("1", "I").replace("0", "O")
        for variant in (code, code.lower(), code.swapcase(), folded, f"  {code}  "):
            async with db_session() as db:
                assert (await get_short_url_content(db, variant)).pubkey == PK, variant

    run(main)


def test_an_unknown_code_is_a_404():
    async def main(minted):
        async with db_session() as db:
            with pytest.raises(HTTPException) as excinfo:
                await get_short_url_content(db, "ZZZZZZZZ")
            assert excinfo.value.status_code == 404

    run(main)
