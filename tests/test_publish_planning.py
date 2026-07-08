"""Publish-decision behaviour driven by real GrapeRank algo results.

`plan_publish` is the pure decision the publish consumer makes from one algo
result + the previously-published set: what to sign, and what each sink deletes.
These tests feed it algo results (including the algorithm's own
`changedScorePubkeys` delta) and assert the publish/delete sets — across a
steady-state delta and a full-sync reconciliation.
"""

from app.message_queue_tasks.upload_nostr_events import plan_publish, prepare_ta_inputs
from app.models.grapeRankResult import GrapeRankResult, ScoreCard

CUTOFF = 0.05


def _result(
    scores: dict[str, float],
    changed: list[str] | None = None,
) -> GrapeRankResult:
    """A GrapeRank result: {observee: influence} plus the algorithm's own delta."""
    cards = {
        pk: ScoreCard(observer="obs", observee=pk, influence=inf)
        for pk, inf in scores.items()
    }
    return GrapeRankResult(
        scorecards=cards,
        duration_seconds=0.0,
        changedScorePubkeys=changed or [],
    )


def test_incremental_delta_run_publishes_only_changed_and_deletes_what_fell_off():
    # Run 1 published {a, b, c}. Run 2: c fell below cutoff, d rose above it.
    run2 = _result(
        {"a": 0.90, "b": 0.80, "c": 0.01, "d": 0.70},
        changed=["c", "d"],
    )

    plan = plan_publish(
        run2,
        previously_published=["a", "b", "c"],
        cutoff=CUTOFF,
        relay_full_sync=False,
        vespa_full_sync=False,
    )

    # Both sinks delete c (it fell off); a/b unchanged, d is new.
    assert plan.relay_deletes == ["c"]
    assert plan.vespa_deletes == ["c"]
    assert sorted(plan.currently_published) == ["a", "b", "d"]
    # Incremental publishes only the changed-and-still-above-cutoff Observee (d);
    # c is "changed" too but below cutoff, so it is signed by nobody — only deleted.
    published = [
        d_tag for d_tag, _, _ in prepare_ta_inputs(run2, CUTOFF, full_sync=False)
    ]
    assert published == ["d"]


def test_local_diff_catches_a_drop_the_algo_failed_to_report():
    # "c" was above cutoff last run, is below it now, but the algorithm reports
    # no delta for it (still present in scorecards, changed=[]). An algo-delta-only
    # delete set would miss it; the local `fell_off` diff deletes it anyway.
    run = _result({"a": 0.90, "c": 0.01}, changed=[])

    plan = plan_publish(
        run,
        previously_published=["a", "c"],
        cutoff=CUTOFF,
        relay_full_sync=False,
        vespa_full_sync=False,
    )

    # fell_off (previously_published - currently_above_cutoff) catches c despite
    # the empty algo delta.
    assert plan.relay_deletes == ["c"]


def test_full_sync_on_10k_publishes_all_above_cutoff_and_sweeps_all_below():
    scores = {f"hi{i}": 0.50 for i in range(6000)}
    scores.update({f"lo{i}": 0.01 for i in range(4000)})
    # No changed hint — full-sync ignores it and reconciles wholesale.
    run = _result(scores, changed=[])

    plan = plan_publish(
        run,
        previously_published=[],
        cutoff=CUTOFF,
        relay_full_sync=True,
        vespa_full_sync=True,
    )

    assert len(plan.currently_published) == 6000
    assert len(plan.relay_deletes) == 4000  # every below-cutoff Observee swept
    assert plan.relay_deletes == plan.vespa_deletes  # matching modes share the set
    published = prepare_ta_inputs(run, CUTOFF, full_sync=True)
    assert len(published) == 6000  # full-sync signs ALL above-cutoff, not just changed


def test_incremental_vs_full_sync_on_same_10k_result():
    scores = {f"hi{i}": 0.50 for i in range(6000)}
    scores.update({f"lo{i}": 0.01 for i in range(4000)})
    run = _result(scores, changed=[])  # a steady-state run: nothing changed

    full = plan_publish(run, [], CUTOFF, relay_full_sync=True, vespa_full_sync=True)
    incr = plan_publish(run, [], CUTOFF, relay_full_sync=False, vespa_full_sync=False)

    # The op-count gap issue 05 is about: full-sync sweeps 4000 deletes + re-signs
    # 6000; the steady-state incremental run touches nothing.
    assert len(full.relay_deletes) == 4000
    assert incr.relay_deletes == []
    assert prepare_ta_inputs(run, CUTOFF, full_sync=False) == []  # nothing to sign


def test_plan_publish_splits_delete_sets_when_sinks_use_different_modes():
    run = _result({"a": 0.90, "lo": 0.01}, changed=[])

    plan = plan_publish(
        run,
        previously_published=["a"],
        cutoff=CUTOFF,
        relay_full_sync=True,
        vespa_full_sync=False,
    )

    assert plan.relay_deletes == ["lo"]  # full-sync sweeps the below-cutoff Observee
    assert plan.vespa_deletes == []  # incremental: nothing fell off the published set
