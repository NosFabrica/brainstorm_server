"""What a user is called in the Redis relationship sets.

The reverse sets (`followed_by:` / `muted_by:` / `reported_by:`) and the forward
sets (`designates:` / `zaps_via:`) name users by pubkey, in the key and in the
members. With SCORES_IN_POSTGRES they name them by User id instead: the same
prefixes, `followed_by:<id>` holding ids. Every reader and writer of those sets
asks here, so the two never mix.
"""
import re
from collections.abc import Iterable

from app.core.config import settings
from app.repos.user_id_repo import ids_for

_PUBKEY = re.compile(r"[0-9a-f]{64}")
_USER_ID = re.compile(r"[0-9]{1,10}")


def names_are_ids() -> bool:
    return settings.scores_in_postgres


def is_set_name(name: str, ids: bool) -> bool:
    """Whether `name` (a key's suffix, or a member) belongs to the id or the pubkey naming."""
    return bool((_USER_ID if ids else _PUBKEY).fullmatch(name))


async def set_names(pubkeys: Iterable[str]) -> dict[str, str]:
    """Each pubkey's name in the sets. With ids, a pubkey that has none (it is in
    no set either) is left out; no id is created here."""
    unique = list(dict.fromkeys(pubkeys))
    if not names_are_ids():
        return {pubkey: pubkey for pubkey in unique}
    known = await ids_for([pk for pk in unique if _PUBKEY.fullmatch(pk)], create=False)
    return {pubkey: str(user_id) for pubkey, user_id in known.items()}


async def set_name(pubkey: str) -> str | None:
    return (await set_names([pubkey])).get(pubkey)
