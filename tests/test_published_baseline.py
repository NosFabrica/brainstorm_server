"""The published-state baseline persisted after a publish run.

`published_state_to_persist` is the pure decision the consumer makes about what
to write into `last_published_pubkeys`. On a clean run it's exactly the intended
above-cutoff set; on a run where a sink write failed (a delete may not have
landed) it must NOT prune past the un-confirmed delete, or that pubkey is
orphaned in the sink forever. Retaining prev ∪ current keeps the delete retried
(idempotent) until a clean run confirms it and prunes back.
"""

from app.message_queue_tasks.upload_nostr_events import (
    plan_publish,
    published_state_to_persist,
)
from app.models.grapeRankResult import GrapeRankResult, ScoreCard

CUTOFF = 0.05


def test_clean_run_persists_exactly_the_current_above_cutoff_set():
    persisted = published_state_to_persist(
        previously_published=["a", "b", "gone"],
        currently_published=["a", "b"],
        sink_write_failed=False,
    )
    # 'gone' fell off and both deletes landed → it's pruned from the baseline.
    assert sorted(persisted) == ["a", "b"]


def test_dirty_run_retains_the_union_so_an_unconfirmed_delete_is_not_orphaned():
    persisted = published_state_to_persist(
        previously_published=["a", "b", "gone"],
        currently_published=["a", "b"],
        sink_write_failed=True,
    )
    # A delete may have silently failed this run → keep 'gone' in the baseline so
    # next run re-issues the (idempotent) delete instead of orphaning it. New
    # above-cutoff keys are still included (a, b).
    assert sorted(persisted) == ["a", "b", "gone"]


def test_dirty_run_still_adds_newly_published_keys_to_the_baseline():
    # 'new' rose above cutoff this run; even on a dirty run it must enter the
    # baseline, else it's untracked and can never be deleted later.
    persisted = published_state_to_persist(
        previously_published=["a"],
        currently_published=["a", "new"],
        sink_write_failed=True,
    )
    assert sorted(persisted) == ["a", "new"]


def test_retained_failed_delete_is_re_deleted_on_the_next_run():
    # End-to-end of the fix: last run failed to delete 'gone' so the coarse
    # variant retained it in the baseline. Next (clean) run's fell_off must
    # include it, so the sink delete is retried.
    run = GrapeRankResult(
        scorecards={"a": ScoreCard(observer="obs", observee="a", influence=0.9)},
        duration_seconds=0.0,
        changedScorePubkeys=[],
    )
    plan = plan_publish(
        run,
        previously_published=["a", "gone"],  # 'gone' retained from a prior dirty run
        cutoff=CUTOFF,
    )
    assert plan.relay_deletes == ["gone"]
    assert plan.vespa_deletes == ["gone"]
