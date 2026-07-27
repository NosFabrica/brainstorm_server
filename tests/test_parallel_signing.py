"""Count-gated parallel signing of Trusted Assertions.

These exercise the pure, process-safe signing seam (`app.message_queue_tasks.
ta_signing`) and the count-gated branch selection in
`get_events_from_graperank_result` — no relay, no DB.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from nostr_sdk import Event, Keys

from app.core.config import settings
from app.message_queue_tasks import upload_nostr_events
from app.message_queue_tasks.ta_signing import (
    UNREACHABLE_HOPS,
    TaInput,
    build_ta_event_builder,
    sign_ta_events_parallel,
    sign_ta_shard,
)
from app.message_queue_tasks.upload_nostr_events import (
    get_events_from_graperank_result,
    get_zero_score_events_for_pubkeys,
    prepare_ta_inputs,
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


def _nsec() -> tuple[str, str]:
    """A fresh (nsec, signing-pubkey-hex) pair."""
    keys = Keys.generate()
    return keys.secret_key().to_bech32(), keys.public_key().to_hex()


def _tags(event: Event) -> dict[str, str]:
    """First value of each tag, keyed by tag name (d/rank/followers/…)."""
    out: dict[str, str] = {}
    for tag in event.tags().to_vec():
        vec = tag.as_vec()
        if len(vec) >= 2 and vec[0] not in out:
            out[vec[0]] = vec[1]
    return out


def test_sign_ta_shard_builds_signed_kind_30382_with_score_tags():
    nsec, pubkey = _nsec()
    inputs = [TaInput("observee-aaa", 73, 12, 3, 5, 2)]

    signed_json = sign_ta_shard(inputs, nsec)

    assert len(signed_json) == 1
    event = Event.from_json(signed_json[0])
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


def test_sign_ta_events_parallel_signs_all_across_a_real_pool():
    nsec, pubkey = _nsec()
    # More inputs than workers so at least one worker handles multiple shards'
    # worth of events, exercising real sharding + reassembly.
    inputs = [TaInput(f"observee-{i:03d}", i % 100, i, 0, 0, 1) for i in range(7)]

    events = asyncio.run(sign_ta_events_parallel(inputs, nsec, max_workers=2))

    assert len(events) == len(inputs)
    assert all(isinstance(ev, Event) for ev in events)
    assert all(ev.verify() and ev.author().to_hex() == pubkey for ev in events)
    # Every requested d-tag is present exactly once (order-independent).
    assert sorted(_tags(ev)["d"] for ev in events) == sorted(
        inp.observee for inp in inputs
    )


def _fake_client(keys: Keys) -> MagicMock:
    """A relay client stub whose `sign_event_builder` signs locally and counts
    its awaits, so a test can tell whether the sequential path was taken."""
    client = MagicMock()

    async def _sign(builder):
        return builder.sign_with_keys(keys)

    client.sign_event_builder = AsyncMock(side_effect=_sign)
    return client


def test_small_run_uses_sequential_client_path_without_spawning_a_pool(monkeypatch):
    keys = Keys.generate()
    nsec = keys.secret_key().to_bech32()
    result = _result([_sc(f"o{i}", 0.5, i) for i in range(3)])
    client = _fake_client(keys)
    monkeypatch.setattr(settings, "sign_parallel_threshold", 10)
    monkeypatch.setattr(settings, "relay_full_sync", True)
    monkeypatch.setattr(settings, "cutoff_of_valid_graperank_scores", 0.05)
    parallel_spy = AsyncMock()
    monkeypatch.setattr(upload_nostr_events, "sign_ta_events_parallel", parallel_spy)

    events = asyncio.run(get_events_from_graperank_result(result, client, nsec))

    assert len(events) == 3
    assert client.sign_event_builder.await_count == 3  # signed via the client
    parallel_spy.assert_not_called()  # no pool for a small run


def test_large_run_in_pool_mode_signs_in_the_pool_not_the_client(monkeypatch):
    keys = Keys.generate()
    nsec = keys.secret_key().to_bech32()
    result = _result([_sc(f"o{i}", 0.5, i) for i in range(3)])
    client = _fake_client(keys)
    monkeypatch.setattr(settings, "sign_parallel_threshold", 2)  # 3 inputs > 2
    monkeypatch.setattr(settings, "relay_full_sync", True)
    monkeypatch.setattr(settings, "cutoff_of_valid_graperank_scores", 0.05)
    pool_events = [
        build_ta_event_builder(inp).sign_with_keys(keys)
        for inp in prepare_ta_inputs(result, 0.05, True)
    ]
    parallel_spy = AsyncMock(return_value=pool_events)
    monkeypatch.setattr(upload_nostr_events, "sign_ta_events_parallel", parallel_spy)

    events = asyncio.run(get_events_from_graperank_result(result, client, nsec))

    assert len(events) == 3
    parallel_spy.assert_awaited_once()  # routed to the pool
    assert client.sign_event_builder.await_count == 0  # client untouched
    # nsec handed to the pool worker (local signing), not a relay client.
    assert parallel_spy.await_args.args[1] == nsec


def _content(event: Event) -> tuple:
    tags = _tags(event)
    return (
        event.kind().as_u16(),
        event.author().to_hex(),
        tags["d"],
        tags["rank"],
        tags["followers"],
        tags["reporters"],
        tags["muters"],
        tags.get("hops"),
    )


def test_sequential_and_pool_paths_produce_content_equivalent_tas(monkeypatch):
    keys = Keys.generate()
    nsec = keys.secret_key().to_bech32()
    # A mix of reachable and unreachable Observees, so both branches must agree on
    # the hops omission too.
    result = _result(
        [
            _sc(
                f"o{i}",
                0.10 + i / 100,
                followers=i,
                reporters=i % 3,
                muters=i % 2,
                hops=UNREACHABLE_HOPS if i % 2 else i + 1,
            )
            for i in range(6)
        ]
    )
    monkeypatch.setattr(settings, "relay_full_sync", True)
    monkeypatch.setattr(settings, "cutoff_of_valid_graperank_scores", 0.05)

    # Sequential branch (threshold above count, client signs).
    monkeypatch.setattr(settings, "sign_parallel_threshold", 100)
    seq = asyncio.run(
        get_events_from_graperank_result(result, _fake_client(keys), nsec)
    )

    # Parallel branch (threshold below count, real process pool signs).
    monkeypatch.setattr(settings, "sign_parallel_threshold", 1)
    monkeypatch.setattr(settings, "sign_parallel_max_workers", 2)
    par = asyncio.run(
        get_events_from_graperank_result(result, _fake_client(keys), nsec)
    )

    # Same kind / signing pubkey / d / rank / counts / hops per observee, across
    # both branches — only the signature and id (non-deterministic) may differ.
    assert {_content(e) for e in seq} == {_content(e) for e in par}
    assert all(e.verify() for e in par)
