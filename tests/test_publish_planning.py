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
    )

    # Both sinks delete c (it fell off); a/b unchanged, d is new.
    assert plan.relay_deletes == ["c"]
    assert plan.vespa_deletes == ["c"]
    assert sorted(plan.currently_published) == ["a", "b", "d"]
    # Incremental publishes only the changed-and-still-above-cutoff Observee (d);
    # c is "changed" too but below cutoff, so it is signed by nobody — only deleted.
    published = [
        inp.observee for inp in prepare_ta_inputs(run2, CUTOFF, full_sync=False)
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
    )

    # fell_off (previously_published - currently_above_cutoff) catches c despite
    # the empty algo delta.
    assert plan.relay_deletes == ["c"]


def test_sweep_on_10k_publishes_all_above_cutoff_and_sweeps_all_below():
    scores = {f"hi{i}": 0.50 for i in range(6000)}
    scores.update({f"lo{i}": 0.01 for i in range(4000)})
    run = _result(scores, changed=[])

    plan = plan_publish(
        run,
        previously_published=[],
        cutoff=CUTOFF,
        sweep_relay=True,
        sweep_vespa=True,
    )

    assert len(plan.currently_published) == 6000
    assert len(plan.relay_deletes) == 4000  # every below-cutoff Observee swept
    assert plan.relay_deletes == plan.vespa_deletes  # matching modes share the set


def test_full_sync_does_not_drive_deletes():
    # Regression: full-sync used to imply the below-cutoff sweep, so every full
    # run emitted kind-5 tombstones for coordinates it had never published.
    # full_sync now only decides re-assertion; the delete set is unaffected.
    scores = {f"hi{i}": 0.50 for i in range(6000)}
    scores.update({f"lo{i}": 0.01 for i in range(4000)})
    run = _result(scores, changed=[])  # a steady-state run: nothing changed

    plan = plan_publish(run, [], CUTOFF)  # sweep off (the default)

    assert plan.relay_deletes == []  # nothing was published, so nothing fell off
    assert plan.vespa_deletes == []
    # ...while full-sync still re-signs every above-cutoff TA.
    assert len(prepare_ta_inputs(run, CUTOFF, full_sync=True)) == 6000
    assert prepare_ta_inputs(run, CUTOFF, full_sync=False) == []  # nothing to sign


def test_sweep_is_additive_to_the_fell_off_diff():
    run = _result({"a": 0.90, "lo": 0.01}, changed=[])

    swept = plan_publish(run, ["a", "x"], CUTOFF, sweep_relay=True, sweep_vespa=True)
    plain = plan_publish(run, ["a", "x"], CUTOFF)

    # "x" fell off the published baseline either way; the sweep adds "lo", which
    # was never published (so its delete is a relay no-op that costs a tombstone).
    assert plain.relay_deletes == ["x"]
    assert swept.relay_deletes == ["lo", "x"]


def test_plan_publish_splits_delete_sets_when_sinks_sweep_differently():
    # Draining Vespa alone: its cheap removes reap "lo" while the relay pays no
    # tombstone for it. Both sinks still delete "x", which genuinely fell off.
    run = _result({"a": 0.90, "lo": 0.01}, changed=[])

    plan = plan_publish(
        run,
        previously_published=["a", "x"],
        cutoff=CUTOFF,
        sweep_relay=False,
        sweep_vespa=True,
    )

    assert plan.relay_deletes == ["x"]  # diff only: no tombstone for "lo"
    assert plan.vespa_deletes == ["lo", "x"]  # swept: the orphan cell goes too


def test_only_the_sweep_reaps_an_orphan():
    # "orph" is below cutoff and was never in the published baseline, so no
    # `fell_off` diff can ever see it. Off (the default) it survives; the drain
    # reaps it on the runs the scheduler already does — no extra work enqueued.
    run = _result({"a": 0.90, "orph": 0.01}, changed=[])

    off = plan_publish(run, ["a"], CUTOFF)
    assert off.relay_deletes == []
    assert off.vespa_deletes == []

    draining = plan_publish(run, ["a"], CUTOFF, sweep_relay=True, sweep_vespa=True)
    assert draining.relay_deletes == ["orph"]
    assert draining.vespa_deletes == ["orph"]
