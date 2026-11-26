"""A run's result names users by pubkey, or, from a worker with scores in
Postgres, by User id: the Observer's pubkey once, `scorecardsById` keyed by the
Observee's id, and id lists. The consumers work in pubkeys, so the id form is
turned into the pubkey form as the message is read.
"""

from app.models.grapeRankResult import GrapeRankResult
from app.repos.user_id_repo import pubkeys_for

LOOKUP_BATCH = 50_000

_ID_LISTS = {
    "changedScoreIds": "changedScorePubkeys",
    "droppedBelowCutoffIds": "droppedBelowCutoffPubkeys",
    "neo4jWriteIds": "neo4jWritePubkeys",
    "neo4jClearIds": "neo4jClearPubkeys",
}


async def name_by_pubkey(result: GrapeRankResult) -> None:
    """Rewrite an id-named result in place; a pubkey-named one is left alone.
    An id without a pubkey fails the run rather than dropping a score."""
    raw: dict = result  # type: ignore[assignment]
    by_id = raw.pop("scorecardsById", None)
    if by_id is None:
        return
    observer = raw.pop("observer")
    wanted = {int(user_id) for user_id in by_id}
    for id_field in _ID_LISTS:
        wanted.update(raw.get(id_field) or ())
    ordered = sorted(wanted)
    pubkeys: dict[int, str] = {}
    for start in range(0, len(ordered), LOOKUP_BATCH):
        pubkeys.update(await pubkeys_for(ordered[start : start + LOOKUP_BATCH]))
    if len(pubkeys) != len(ordered):
        unknown = [user_id for user_id in ordered if user_id not in pubkeys]
        raise RuntimeError(
            f"{len(unknown)} user ids in the result have no pubkey: {unknown[:5]}"
        )

    scorecards = {}
    for user_id, card in by_id.items():
        observee = pubkeys[int(user_id)]
        card["observer"] = observer
        card["observee"] = observee
        scorecards[observee] = card
    raw["scorecards"] = scorecards
    for id_field, pubkey_field in _ID_LISTS.items():
        ids = raw.pop(id_field, None)
        # Absent or null keeps its meaning (the write list: "write everything").
        raw[pubkey_field] = None if ids is None else [pubkeys[i] for i in ids]
