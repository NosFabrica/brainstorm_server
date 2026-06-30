"""Admin reconcile: diff actual published state vs Neo4j desired state.

Pure drift classification for a single observer/sink — the brains of the
on-demand reconcile. The relay stream, Vespa GET-loop, Neo4j batch read, apply,
and endpoint are I/O wired on top of these.
"""

from app.services.reconcile import (
    RelayDriftAccumulator,
    build_desired_map,
    classify_relay_drift,
    classify_vespa_cell,
    summarize_drift,
)


def test_classify_relay_drift_categorizes_match_stale_ghost_and_missing():
    # desired = {observee: expected rank}. actual = streamed (observee, rank).
    desired = {"keep": 90, "drift": 50, "gone": 30}
    actual = [("keep", 90), ("drift", 47), ("ghost", 10)]

    result = classify_relay_drift(desired, actual)

    # "keep" matches → no correction. "drift" rank differs → stale, republish at
    # the DESIRED rank. "ghost" isn't desired → delete. "gone" never appeared in
    # the stream → missing, publish at desired rank.
    assert result.stale == [("drift", 50)]
    assert result.ghost == ["ghost"]
    assert result.missing == [("gone", 30)]


def test_classify_relay_drift_dedupes_repeated_actual_events():
    # The relay may stream the same coordinate more than once (un-replaced
    # historical copies). A second sighting of an already-classified Observee
    # must not double-count — neither a matched one nor a ghost.
    desired = {"keep": 90}
    actual = [("keep", 90), ("keep", 90), ("ghost", 10), ("ghost", 10)]

    result = classify_relay_drift(desired, actual)

    assert result.ghost == ["ghost"]  # once, not twice
    assert result.stale == []
    assert result.missing == []


def test_relay_drift_accumulator_classifies_streamed_one_at_a_time():
    # The streaming shape: events are pushed in as they arrive off the relay and
    # dropped (never buffered), then result() reports leftovers as missing. Same
    # classification as the batch form, incl. dedupe of a re-streamed coordinate.
    acc = RelayDriftAccumulator({"keep": 90, "drift": 50, "gone": 30})

    acc.observe("keep", 90)
    acc.observe("drift", 47)
    acc.observe("ghost", 10)
    acc.observe("ghost", 10)  # re-streamed copy → counted once

    result = acc.result()
    assert result.stale == [("drift", 50)]
    assert result.ghost == ["ghost"]
    assert result.missing == [("gone", 30)]


def test_classify_vespa_cell():
    # A desired Observee's tensor cell for this observer: absent → missing,
    # present-but-wrong → stale, present-and-equal → match.
    assert classify_vespa_cell(expected_rank=90, actual_cell=None) == "missing"
    assert classify_vespa_cell(expected_rank=90, actual_cell=47) == "stale"
    assert classify_vespa_cell(expected_rank=90, actual_cell=90) == "match"


def test_build_desired_map_filters_below_cutoff_and_ranks_above():
    # rows = (observee, influence) from Neo4j. rank = round(influence*100); only
    # above-cutoff kept; null influence (property unset) skipped.
    rows = [("hi", 0.90), ("mid", 0.40), ("low", 0.02), ("unset", None)]

    desired = build_desired_map(rows, cutoff=0.05)

    assert desired == {"hi": 90, "mid": 40}


def test_summarize_drift_counts_and_truncates_to_first_100():
    missing = [f"m{i}" for i in range(60)]
    stale = [f"s{i}" for i in range(50)]
    ghost = ["g0"]

    report = summarize_drift(missing, stale, ghost, limit=100)

    assert report["counts"] == {"missing": 60, "stale": 50, "ghost": 1}
    assert report["total"] == 111
    assert len(report["mismatches"]) == 100  # capped
    assert report["truncated"] is True


def test_summarize_drift_full_returns_every_mismatch():
    report = summarize_drift(["m0"], ["s0"], ["g0"], limit=100, full=True)

    assert report["total"] == 3
    assert len(report["mismatches"]) == 3
    assert report["truncated"] is False
    assert {m["kind"] for m in report["mismatches"]} == {"missing", "stale", "ghost"}
