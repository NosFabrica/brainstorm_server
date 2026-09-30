"""Trusted Assertion and deletion signing — the pure signing seam
(`app.message_queue_tasks.ta_signing`) and the streaming producer the publisher
consumes. No relay, no DB.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from nostr_sdk import Event, Keys

from app.message_queue_tasks.ta_signing import (
    UNREACHABLE_HOPS,
    TaInput,
    atag_deletion_tags,
    build_atag_deletion_builders,
    build_ta_event_builder,
    sign_deletion_json,
    sign_ta_json,
)
from app.message_queue_tasks.upload_nostr_events import (
    SIGN_CHUNK,
    get_zero_score_events_for_pubkeys,
    prepare_ta_inputs,
    sign_publish_events,
)
from app.models.grapeRankResult import GrapeRankResult, ScoreCard


def _result(scorecards: list[ScoreCard], changed: list[str] | None = None):
    return GrapeRankResult(
        scorecards={sc.observee: sc for sc in scorecards},
        duration_seconds=0.0,
        changedScorePubkeys=changed or [],
    )


def _sc(
    observee: str,
    influence: float,
    followers: int = 0,
    reporters: int = 0,
    muters: int = 0,
    hops: int = 1,
) -> ScoreCard:
    return ScoreCard(
        observer="obs",
        observee=observee,
        influence=influence,
        trusted_followers=followers,
        trusted_reporters=reporters,
        trusted_muters=muters,
        hops=hops,
    )


def _tags(event: Event) -> dict[str, str]:
    """First value of each tag, keyed by tag name (d/rank/followers/…)."""
    out: dict[str, str] = {}
    for tag in event.tags().to_vec():
        vec = tag.as_vec()
        if len(vec) >= 2 and vec[0] not in out:
            out[vec[0]] = vec[1]
    return out


def test_sign_ta_json_builds_signed_kind_30382_with_score_tags():
    keys = Keys.generate()
    pubkey = keys.public_key().to_hex()

    signed = sign_ta_json(TaInput("observee-aaa", 73, 12, 3, 5, 2), keys, pubkey)

    event = Event.from_json(signed.json)
    assert signed.id == event.id().to_hex()
    assert event.verify()  # valid schnorr signature
    assert event.kind().as_u16() == 30382
    assert event.author().to_hex() == pubkey
    assert _tags(event) == {
        "d": "observee-aaa",
        "rank": "73",
        "followers": "12",
        "reporters": "3",
        "muters": "5",
        "hops": "2",
        "client": "Brainstorm",
    }


def test_ta_omits_hops_at_the_unreachable_sentinel():
    # A consuming client should never have to special-case "999 means no path" —
    # the tag is simply absent. The guard keys on the sentinel, not on the
    # algorithm's current hop limit, so raising that limit needs no edit here.
    reachable = build_ta_event_builder(TaInput("o", 50, 1, 0, 0, UNREACHABLE_HOPS - 1))
    unreachable = build_ta_event_builder(TaInput("o", 50, 1, 0, 0, UNREACHABLE_HOPS))
    keys = Keys.generate()

    assert _tags(reachable.sign_with_keys(keys))["hops"] == str(UNREACHABLE_HOPS - 1)
    assert "hops" not in _tags(unreachable.sign_with_keys(keys))


def test_sign_ta_json_matches_the_sdk_builder_and_serializes_per_nip01():
    # The id is hashed in Python, so it must match nostr-sdk's own NIP-01
    # serialization byte for byte — including the escapes a d-tag could carry.
    keys = Keys.generate()
    pubkey = keys.public_key().to_hex()
    for observee in ["a" * 64, 'é "q" \\ \n\t\r\b\f \x01 \u2028 😀']:
        for hops in (2, UNREACHABLE_HOPS):
            ta_input = TaInput(observee, 73, 12, 3, 5, hops)
            ours = Event.from_json(sign_ta_json(ta_input, keys, pubkey).json)
            sdks = build_ta_event_builder(ta_input).sign_with_keys(keys)

            assert ours.verify()  # id recomputed by nostr-sdk, then the signature
            assert ours.kind().as_u16() == 30382
            assert ours.author().to_hex() == pubkey
            assert ours.content() == ""
            assert [t.as_vec() for t in ours.tags().to_vec()] == [
                t.as_vec() for t in sdks.tags().to_vec()
            ]


def test_zero_score_events_carry_zero_counts_and_no_hops():
    keys = Keys.generate()

    events = asyncio.run(get_zero_score_events_for_pubkeys(["gone"], _fake_client(keys)))

    assert len(events) == 1
    assert events[0].kind().as_u16() == 30382
    assert _tags(events[0]) == {
        "d": "gone",
        "rank": "0",
        "followers": "0",
        "reporters": "0",
        "muters": "0",
        "client": "Brainstorm",
    }


def test_prepare_ta_inputs_drops_below_cutoff_and_sorts_by_influence_desc():
    result = _result([_sc("low", 0.02, 1), _sc("hi", 0.90, 2), _sc("mid", 0.40, 3)])

    inputs = prepare_ta_inputs(result, cutoff=0.05, full_sync=True)

    # below-cutoff "low" dropped; remainder sorted by influence descending;
    # rank = round(influence*100), followers passed through.
    assert inputs == [("hi", 90, 2, 0, 0, 1), ("mid", 40, 3, 0, 0, 1)]


def test_prepare_ta_inputs_carries_the_scorecards_counts_and_hops():
    result = _result([_sc("a", 0.90, followers=7, reporters=2, muters=4, hops=3)])

    (inp,) = prepare_ta_inputs(result, cutoff=0.05, full_sync=True)

    assert (inp.followers, inp.reporters, inp.muters, inp.hops) == (7, 2, 4, 3)


def test_prepare_ta_inputs_incremental_keeps_only_changed_pubkeys():
    result = _result([_sc("a", 0.90), _sc("b", 0.80), _sc("c", 0.70)], changed=["b"])

    inputs = prepare_ta_inputs(result, cutoff=0.05, full_sync=False)

    assert [inp.observee for inp in inputs] == ["b"]


def _fake_client(keys: Keys) -> MagicMock:
    """A relay client stub whose `sign_event_builder` signs locally and counts
    its awaits, so a test can tell whether the sequential path was taken."""
    client = MagicMock()

    async def _sign(builder):
        return builder.sign_with_keys(keys)

    client.sign_event_builder = AsyncMock(side_effect=_sign)
    return client


def test_json_deletions_match_the_sdk_builders():
    keys = Keys.generate()
    pubkey = keys.public_key().to_hex()
    observees = ["observee-a", "observee-b", "observee-c"]

    ours = [
        Event.from_json(sign_deletion_json(tags, keys, pubkey).json)
        for tags in atag_deletion_tags(observees, pubkey, chunk_size=2)
    ]
    sdks = [
        b.sign_with_keys(keys)
        for b in build_atag_deletion_builders(observees, pubkey, chunk_size=2)
    ]

    assert len(ours) == 2
    for mine, theirs in zip(ours, sdks, strict=True):
        assert mine.verify()
        assert mine.kind().as_u16() == 5
        assert mine.content() == theirs.content()
        assert [t.as_vec() for t in mine.tags().to_vec()] == [
            t.as_vec() for t in theirs.tags().to_vec()
        ]


async def _collect(gen):
    return [e async for e in gen]


def test_sign_publish_events_streams_tas_then_deletions():
    keys = Keys.generate()
    pubkey = keys.public_key().to_hex()
    # More than one chunk, so the chunk boundary is crossed.
    inputs = [TaInput(f"o{i}", i, i, 0, 0, 1) for i in range(SIGN_CHUNK + 3)]
    timing: dict[str, float] = {}

    events = asyncio.run(
        _collect(sign_publish_events(inputs, ["gone-1", "gone-2"], keys, timing))
    )

    parsed = [Event.from_json(e.json) for e in events]
    assert len(parsed) == len(inputs) + 1  # one kind-5 carries both coordinates
    assert all(e.verify() and e.author().to_hex() == pubkey for e in parsed)
    assert [e.id for e in events] == [p.id().to_hex() for p in parsed]
    assert [_tags(p)["d"] for p in parsed[:-1]] == [i.observee for i in inputs]
    deletion = parsed[-1]
    assert deletion.kind().as_u16() == 5
    assert [t.as_vec()[1] for t in deletion.tags().to_vec() if t.as_vec()[0] == "a"] == [
        f"30382:{pubkey}:gone-1",
        f"30382:{pubkey}:gone-2",
    ]
    assert timing["sign_cpu"] > 0


def test_sign_publish_events_with_nothing_to_publish_yields_nothing():
    events = asyncio.run(
        _collect(sign_publish_events([], [], Keys.generate(), {}))
    )

    assert events == []
