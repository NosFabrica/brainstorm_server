"""Pubkey ↔ User id translation. The only code that touches `nostr_user`.

Each call runs in its own committed transaction, so an id is durable before
the caller writes it to Neo4j.
"""
import re
from typing import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, INTEGER, TEXT

from app.core.scorecard_database import scorecard_session

_PUBKEY = re.compile(r"[0-9a-f]{64}")

# Only missing pubkeys reach the INSERT, so a known one never draws from the
# sequence. Sorted, so overlapping batches lock index entries in one order.
_CREATE = text(
    """
    WITH input AS (
        SELECT DISTINCT pubkey FROM unnest(:pubkeys) AS t(pubkey)
    ), inserted AS (
        INSERT INTO nostr_user (pubkey)
        SELECT i.pubkey FROM input i
        WHERE NOT EXISTS (SELECT 1 FROM nostr_user u WHERE u.pubkey = i.pubkey)
        ORDER BY i.pubkey
        ON CONFLICT (pubkey) DO NOTHING
        RETURNING id, pubkey
    )
    SELECT id, pubkey FROM inserted
    UNION ALL
    SELECT u.id, u.pubkey FROM nostr_user u JOIN input i USING (pubkey)
    """
).bindparams(bindparam("pubkeys", type_=ARRAY(TEXT)))

_READ = text(
    "SELECT id, pubkey FROM nostr_user WHERE pubkey = ANY(:pubkeys)"
).bindparams(bindparam("pubkeys", type_=ARRAY(TEXT)))

_READ_BY_ID = text("SELECT id, pubkey FROM nostr_user WHERE id = ANY(:ids)").bindparams(
    bindparam("ids", type_=ARRAY(INTEGER))
)


def _validated(pubkeys: Iterable[str]) -> list[str]:
    unique = list(dict.fromkeys(pubkeys))
    for pubkey in unique:
        if not isinstance(pubkey, str) or not _PUBKEY.fullmatch(pubkey):
            raise ValueError(f"not a 64-char lowercase hex pubkey: {pubkey!r:.80}")
    return unique


async def ids_for(pubkeys: Iterable[str], create: bool = True) -> dict[str, int]:
    """User ids by pubkey. With `create`, a pubkey without one is given one;
    without it, unknown pubkeys are left out."""
    wanted = _validated(pubkeys)
    if not wanted:
        return {}
    async with scorecard_session() as db:
        rows = await db.execute(_CREATE if create else _READ, {"pubkeys": wanted})
        found = {pubkey: user_id for user_id, pubkey in rows}
    if not create or len(found) == len(wanted):
        return found
    # A concurrent caller inserted these first; its rows were not in our snapshot.
    missing = [pubkey for pubkey in wanted if pubkey not in found]
    async with scorecard_session() as db:
        rows = await db.execute(_READ, {"pubkeys": missing})
        found.update({pubkey: user_id for user_id, pubkey in rows})
    if len(found) != len(wanted):
        raise RuntimeError(f"{len(wanted) - len(found)} pubkeys were given no user id")
    return found


async def pubkeys_for(user_ids: Iterable[int]) -> dict[int, str]:
    """Pubkeys by User id. Unknown ids are left out."""
    wanted = list(dict.fromkeys(user_ids))
    if not wanted:
        return {}
    async with scorecard_session() as db:
        rows = await db.execute(_READ_BY_ID, {"ids": wanted})
        return {user_id: pubkey for user_id, pubkey in rows}
