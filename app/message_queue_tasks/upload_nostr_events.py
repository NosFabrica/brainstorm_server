import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import NamedTuple

from nostr_sdk import Client, Event, Keys, NostrSigner  # type: ignore

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.db_models import BrainstormRequestStatus
from app.message_queue_tasks.loop_probe import LoopProbe
from app.message_queue_tasks.relay_publisher import PublishStats, publish_events
from app.message_queue_tasks.ta_signing import (
    DELETION_COORDS_PER_EVENT,
    UNREACHABLE_HOPS,
    SignedEvent,
    TaInput,
    atag_deletion_tags,
    build_atag_deletion_builders,
    build_ta_event_builder,
    sign_deletion_json,
    sign_ta_json,
)
from app.models.grapeRankResult import GrapeRankResult
from app.repos.brainstorm_nsec import (
    get_last_published_pubkeys_by_pubkey_on_db,
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
    reset_runs_since_full_on_db,
    update_assistant_kind0_published_at_on_db,
    update_last_published_pubkeys_by_pubkey_on_db,
    update_last_time_published_graperank_on_db,
)
from app.repos.brainstorm_request_repo import (
    mark_brainstorm_request_force_full_relay_on_db,
    relay_publish_gap_pending_on_db,
    select_brainstorm_request_by_id_on_db,
    update_brainstorm_request_publish_duration_by_id_on_db,
    update_brainstorm_request_ta_status_by_id_on_db,
)
from app.services.assistant_profile_service import publish_assistant_kind0_for_user
from app.services.publish_drift import resolve_full_sync

logger = loggr.get_logger(__name__)


def prepare_ta_inputs(
    grape_rank_result: GrapeRankResult,
    cutoff: float,
    full_sync: bool,
) -> list[TaInput]:
    """The above-cutoff scorecards to assert, as picklable `TaInput`s — sorted by
    influence descending.

    Replicates the two filters of the original sign loop: the full-sync /
    changed-score gate (which scorecards to consider this run), then the
    per-scorecard influence cutoff."""
    scorecards = grape_rank_result["scorecards"]
    assert scorecards is not None
    changed_pubkeys = set(grape_rank_result.get("changedScorePubkeys", []))
    selected = sorted(
        (
            sc
            for pubkey, sc in scorecards.items()
            if full_sync or pubkey in changed_pubkeys
        ),
        key=lambda sc: sc["influence"],
        reverse=True,
    )
    # int(): a pre-cutover worker sends counts/hops as floats; tags must read "3", not "3.0"
    return [
        TaInput(
            sc["observee"],
            round(sc["influence"] * 100),
            int(sc.get("trusted_followers", 0)),
            int(sc.get("trusted_reporters", 0)),
            int(sc.get("trusted_muters", 0)),
            int(sc["hops"]),
        )
        for sc in selected
        if round(sc["influence"], 2) >= cutoff
    ]


def _delete_from_sets(
    fell_off: set[str], below_cutoff: set[str], sweep_below_cutoff: bool
) -> list[str]:
    """The delete set from pre-built sets: Observees that fell off
    (`previously - currently`), plus the whole below-cutoff sweep when enabled."""
    return sorted(fell_off | below_cutoff if sweep_below_cutoff else fell_off)


def compute_delete_observees(
    previously_published: list[str],
    currently_published: list[str],
    below_cutoff: list[str],
    sweep_below_cutoff: bool,
) -> list[str]:
    """The relay's delete set — computed from a local diff, no relay read.

    Default: `previously_published - currently_published` (everything we'd
    published that is no longer above cutoff; this subsumes both genuine removals
    and below-cutoff drops, including ones the algo never reported).

    `sweep_below_cutoff` additionally deletes every below-cutoff Observee, reaping
    orphans the diff can't see (legacy sub-cutoff rows, best-effort-write
    leftovers). Nearly all of those were never published, so the sweep is mostly
    no-op deletes that still cost a kind-5 tombstone — hence its own gate
    (`settings.relay_sweep_below_cutoff`), NOT tied to full-sync: full-sync
    re-asserts, it does not decide what to delete."""
    return _delete_from_sets(
        set(previously_published) - set(currently_published),
        set(below_cutoff),
        sweep_below_cutoff,
    )


class PublishPlan(NamedTuple):
    """What one publish run does with an algo result, decided up front.

    `currently_published` is every above-cutoff Observee (the new published-state
    to persist); `deletes` is the relay delete set."""

    currently_published: list[str]
    deletes: list[str]


def plan_publish(
    grape_rank_result: GrapeRankResult,
    previously_published: list[str],
    cutoff: float,
    sweep_below_cutoff: bool = False,
) -> PublishPlan:
    """Decide the delete set + the new published-state from one algo result, with
    no relay read. Pure, so the whole publish decision is testable against algo
    results without DB/relay/signing.

    The delete set does NOT depend on full-sync — that decides what is
    re-asserted, not what is deleted (see `compute_delete_observees`)."""
    scorecards = grape_rank_result["scorecards"]
    assert scorecards is not None
    currently_published: list[str] = []
    above: set[str] = set()
    below: set[str] = set()
    for sc in scorecards.values():
        if round(sc["influence"], 2) >= cutoff:
            currently_published.append(sc["observee"])
            above.add(sc["observee"])
        else:
            below.add(sc["observee"])

    fell_off = set(previously_published) - above
    return PublishPlan(
        currently_published, _delete_from_sets(fell_off, below, sweep_below_cutoff)
    )


def published_state_to_persist(
    previously_published: list[str],
    currently_published: list[str],
    relay_write_failed: bool,
) -> list[str]:
    """The `last_published_pubkeys` baseline to persist after a run.

    Clean run → exactly the intended above-cutoff set. But if a relay write
    failed this run, a delete may not have landed, and pruning the baseline past
    it would orphan that pubkey on the relay forever (no future `fell_off` could
    ever see it). So on a dirty run keep prev + current: the un-confirmed delete
    stays in the baseline and is re-issued (idempotently) next run, while newly
    above-cutoff keys are still tracked. A later clean run prunes back to
    `currently_published`, so the baseline self-corrects and can't grow unbounded.

    Coarse on purpose — it keys on "any relay failure", not on which delete
    failed, so it over-retains slightly (a redundant, harmless re-delete). This
    only prevents NEW orphans; orphans already on the relay need the reset sweep."""
    if relay_write_failed:
        return list(set(previously_published) | set(currently_published))
    return currently_published


RELAYS: list[str] = [
    x
    for x in [
        settings.nostr_upload_ta_events_relay,
        # settings.nostr_transfer_to_relay2,
    ]
    if x
]

# TAs signed between event-loop yields (~1.6ms of signing per chunk).
SIGN_CHUNK = 50


@contextmanager
def _timed(timings: dict[str, float], name: str):
    """Record wall-clock seconds for a segment into `timings`. Works around an
    `await` inside the block — __exit__ runs after the awaited call resumes."""
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round(time.perf_counter() - start, 3)


def _log_publish_timing(
    run_id,
    observer: str,
    timings: dict[str, float],
    counts: dict[str, int | float],
    run_start: float,
    error: str | None = None,
) -> None:
    """One structured per-run summary line attributing the publish wall-clock across
    segments. Emitted on both the success and failure paths so slow/failed runs are
    still attributed (completed segments reveal where a failure happened)."""
    total = round(time.perf_counter() - run_start, 3)
    seg_str = " ".join(f"{k}={v}s" for k, v in timings.items())
    cnt_str = " ".join(f"{k}={v}" for k, v in counts.items())
    extra = {
        "run_id": run_id,
        "observer": observer,
        "total_s": total,
        **{f"t_{k}": v for k, v in timings.items()},
        **counts,
    }
    if error is not None:
        extra["error"] = error
        logger.error(
            f"TA publish timing (FAILED) run={run_id} observer={observer} "
            f"total={total}s | {seg_str} | {cnt_str} | error={error}",
            extra=extra,
        )
    else:
        logger.info(
            f"TA publish timing run={run_id} observer={observer} "
            f"total={total}s | {seg_str} | {cnt_str}",
            extra=extra,
        )


async def init_nostr_client(secret_key_nsec: str) -> Client:
    logger.info("Starting Nostr client...")
    keys: Keys = Keys.parse(secret_key=secret_key_nsec)
    signer: NostrSigner = NostrSigner.keys(keys=keys)
    client = Client(signer=signer)
    relay_count: int = 0
    for relay in RELAYS:
        logger.info(f"Adding relay {relay}")
        try:
            await client.add_relay(relay)
            relay_count += 1
        except Exception:
            logger.error(f"Bad relay {relay}")
    if relay_count == 0:
        raise Exception("No good relay available, shutting down!")

    logger.info("Finished adding relays!")
    result = await client.try_connect(timedelta(seconds=10))
    assert not bool(result.failed)
    logger.info("Nostr Client Connected!!!")

    return client


def get_ta_inputs(
    grape_rank_result: GrapeRankResult,
    relay_full_sync: bool | None = None,
) -> list[TaInput]:
    assert grape_rank_result["scorecards"] is not None
    # Resolved per-run mode (settings default OR a per-run force_full override);
    # callers without an override pass None and get the env default.
    if relay_full_sync is None:
        relay_full_sync = settings.relay_full_sync
    changed_pubkeys = set(grape_rank_result.get("changedScorePubkeys", []))
    # relay_full_sync=True republishes every above-cutoff TA (reconciliation /
    # drift correction); False publishes only changed scores (steady state).
    logger.info(
        f"relay_full_sync={relay_full_sync}: publishing "
        f"{'all above-cutoff' if relay_full_sync else f'{len(changed_pubkeys)} changed-score'} "
        f"pubkeys out of {len(grape_rank_result['scorecards'])} scorecards"
    )
    return prepare_ta_inputs(
        grape_rank_result,
        cutoff=settings.cutoff_of_valid_graperank_scores,
        full_sync=relay_full_sync,
    )


async def sign_publish_events(
    inputs: list[TaInput],
    deletion_observees: list[str],
    keys: Keys,
    sign_timing: dict[str, float],
) -> AsyncIterator[SignedEvent]:
    """The run's TAs, then its kind-5 deletions, signed lazily in chunks so only
    the events the publisher hasn't confirmed yet are ever held in memory."""
    pubkey_hex = keys.public_key().to_hex()
    sign_timing.setdefault("sign_cpu", 0.0)
    for start in range(0, len(inputs), SIGN_CHUNK):
        t = time.perf_counter()
        batch = [
            sign_ta_json(i, keys, pubkey_hex)
            for i in inputs[start : start + SIGN_CHUNK]
        ]
        sign_timing["sign_cpu"] += time.perf_counter() - t
        for event in batch:
            yield event
        await asyncio.sleep(0)
    for tags in atag_deletion_tags(deletion_observees, pubkey_hex):
        yield sign_deletion_json(tags, keys, pubkey_hex)


def _publish_counts(stats: PublishStats) -> dict[str, int]:
    return {
        "n_published": stats.n_events,
        "n_acked": stats.n_acked,
        "n_failed": stats.n_failed,
        "n_retried": stats.n_retried,
        "n_reconnects": stats.n_reconnects,
        **{f"n_rejected_{reason}": n for reason, n in stats.rejected.items()},
    }


async def get_zero_score_events_for_pubkeys(
    pubkeys: list[str],
    nostr_client: Client,
) -> list[Event]:
    # Replaceable kind 30382 events with rank=0. Published before the kind 5
    # deletions so that even if the relay rejects kind 5, the score is
    # effectively zeroed out. Built through the shared builder so a zeroed event
    # carries the same tag set as a live one — every count zero, and no `hops`
    # (a dropped Observee has no asserted distance).
    events: list[Event] = []
    for pk in pubkeys:
        builder = build_ta_event_builder(TaInput(pk, 0, 0, 0, 0, UNREACHABLE_HOPS))
        signed_event = await nostr_client.sign_event_builder(builder)
        events.append(signed_event)
    return events


async def get_deletion_events_for_dropped_pubkeys(
    observees: list[str],
    signing_pubkey: str,
    nostr_client: Client,
) -> list[Event]:
    """Signed kind-5 deletions that remove each dropped Observee's TA by `a`-tag
    coordinate — no relay fetch (the coordinate `30382:<signing_pubkey>:<observee>`
    is reconstructable, so we never look up the existing event ids)."""
    if not observees:
        logger.info("zero dropped Observees. no events will be deleted")
        return []

    logger.info(
        f"building a-tag kind-5 deletions for {len(observees)} dropped Observees"
    )
    builders = build_atag_deletion_builders(observees, signing_pubkey)
    return [await nostr_client.sign_event_builder(b) for b in builders]


async def ensure_assistant_kind0_published(
    observer: str,
    assistant_kind0_published_at: datetime | None,
    timings: dict[str, float],
) -> None:
    """An Assistant that has never published its kind-0 gets it now, BEFORE its
    first TA batch — scores must never be authored by a profile-less key. Only
    the in-app flow calls POST /user/assistantProfile; NIP-07 users reach this
    consumer with no profile anywhere. Best-effort: on failure the flag stays
    unset so the next run retries, and the TA publish itself is never blocked.
    """
    if assistant_kind0_published_at is not None:
        return
    with _timed(timings, "kind0_profile"):
        try:
            async with db_session() as db:
                await publish_assistant_kind0_for_user(db, user_pubkey=observer)
                await update_assistant_kind0_published_at_on_db(db, pubkey=observer)
                await db.commit()
        except Exception as e:
            logger.warning(
                f"assistant kind-0 publish failed for observer {observer}: {e}"
            )


async def process_nostr_upload_message(message: dict):
    # is_success = message["result"]["success"]

    # if not is_success:
    #     return

    grape_rank_result: GrapeRankResult = message["result"]
    scorecards = grape_rank_result.get("scorecards")
    if not scorecards:
        return
    observer = next(iter(scorecards.values()))["observer"]
    run_id = message["private_id"]
    timings: dict[str, float] = {}
    counts: dict[str, int | float] = {"n_scorecards": len(scorecards)}
    run_start = time.perf_counter()

    # TODO: generate a new nsec for the observer of the observer
    with _timed(timings, "nsec_lookup"):
        async with db_session() as db:
            # Sub-timers to localise the (highly variable, 2-6s) nsec_lookup cost.
            # Each still carries the _timed blind spot, but together they isolate
            # which await dominates: the SELECT, the status UPDATE, or the commit.
            with _timed(timings, "nsec_select"):
                (
                    nsec_db_obj,
                    _was_created_now,
                ) = await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
                    db, pubkey=observer
                )
            assert nsec_db_obj.pubkey == observer
            # Captured while the session is live — read again below to decide
            # whether this Assistant still needs its kind-0 profile published.
            assistant_kind0_published_at = nsec_db_obj.assistant_kind0_published_at
            # This run's force-full override (drift repair), read in the
            # existing session. settings default OR the per-run override.
            request_row = await select_brainstorm_request_by_id_on_db(db, run_id)
            relay_full_sync = resolve_full_sync(
                settings.relay_full_sync, request_row.force_full_relay
            )
            if not relay_full_sync and await relay_publish_gap_pending_on_db(
                db, observer, run_id
            ):
                logger.warning(
                    f"TA publish: an earlier run failed to publish; forcing a full "
                    f"relay sync (run={run_id} observer={observer})"
                )
                relay_full_sync = True
                await mark_brainstorm_request_force_full_relay_on_db(db, run_id)
            with _timed(timings, "nsec_ta_update"):
                await update_brainstorm_request_ta_status_by_id_on_db(
                    db,
                    brainstorm_request_id=run_id,
                    status=BrainstormRequestStatus.ONGOING,
                )
            with _timed(timings, "nsec_commit"):
                await db.commit()

    try:
        keys = Keys.parse(secret_key=nsec_db_obj.nsec)

        await ensure_assistant_kind0_published(
            observer, assistant_kind0_published_at, timings
        )

        with _timed(timings, "prepare"):
            ta_inputs = get_ta_inputs(
                grape_rank_result, relay_full_sync=relay_full_sync
            )
        counts["n_signed"] = len(ta_inputs)

        with _timed(timings, "last_published"):
            async with db_session() as db:
                previously_published_pubkeys = (
                    await get_last_published_pubkeys_by_pubkey_on_db(
                        db, pubkey=observer
                    )
                )

        with _timed(timings, "compute_deletes"):
            # Fetch-free local-diff delete set; catches drops the algo never
            # reported. Independent of full-sync: that re-asserts, it does not delete.
            plan = plan_publish(
                grape_rank_result,
                previously_published=previously_published_pubkeys,
                cutoff=settings.cutoff_of_valid_graperank_scores,
                sweep_below_cutoff=settings.relay_sweep_below_cutoff,
            )
            currently_published_pubkeys = plan.currently_published
        # A sweep is a backlog-draining mode, not a steady state — say so loudly
        # so it can't sit on unnoticed, billing no-op deletes every run.
        if settings.relay_sweep_below_cutoff:
            logger.warning(
                f"below-cutoff sweep ENABLED for observer {observer}: every run "
                f"reaps every below-cutoff Observee. Turn RELAY_SWEEP_BELOW_CUTOFF "
                f"back off once the orphan backlog has drained."
            )
        counts["n_above_cutoff"] = len(currently_published_pubkeys)
        counts["n_relay_deletes"] = len(plan.deletes)
        counts["n_deletion_events"] = -(-len(plan.deletes) // DELETION_COORDS_PER_EVENT)

        # Signed and sent as one stream; the run fails unless the relay OKs every
        # event, so last_published_pubkeys never records an unconfirmed TA.
        sign_timing: dict[str, float] = {}
        with _timed(timings, "publish"):
            async with LoopProbe() as probe:
                publish_stats = await publish_events(
                    settings.nostr_upload_ta_events_relay,
                    sign_publish_events(ta_inputs, plan.deletes, keys, sign_timing),
                )
        timings["acked"] = publish_stats.t_acked
        timings["sign_cpu"] = round(sign_timing.get("sign_cpu", 0.0), 3)
        counts.update(_publish_counts(publish_stats))
        counts.update(probe.summary())
        if publish_stats.aborted or publish_stats.n_unacked:
            raise RuntimeError(
                f"relay publish incomplete: {publish_stats.n_acked}/"
                f"{publish_stats.n_events} events acked (aborted="
                f"{publish_stats.aborted}, samples={publish_stats.failure_samples})"
            )

        with _timed(timings, "final_db"):
            async with db_session() as db:
                await update_brainstorm_request_ta_status_by_id_on_db(
                    db,
                    brainstorm_request_id=run_id,
                    status=BrainstormRequestStatus.SUCCESS,
                )

                # The relay can't be dirty here (unacked → raised above), so the
                # baseline is exactly this run's above-cutoff set.
                await update_last_published_pubkeys_by_pubkey_on_db(
                    db,
                    pubkey=observer,
                    published_pubkeys=currently_published_pubkeys,
                    graperank_request_id=run_id,
                )

                # Freshness clock for the scheduler — advanced only on success.
                await update_last_time_published_graperank_on_db(db, pubkey=observer)

                # Measured publish duration for the scheduler's throughput metrics.
                await update_brainstorm_request_publish_duration_by_id_on_db(
                    db, run_id, round(time.perf_counter() - run_start, 3)
                )

                # A successful full run brings the observer in sync → clear the
                # every-Nth backstop counter (incl. an admin resync).
                if relay_full_sync:
                    await reset_runs_since_full_on_db(db, pubkey=observer)

                await db.commit()

        _log_publish_timing(run_id, observer, timings, counts, run_start)
    except Exception as e:
        logger.error(f"Error on request {run_id} , {e}")
        _log_publish_timing(run_id, observer, timings, counts, run_start, error=str(e))
        async with db_session() as db:
            await update_brainstorm_request_ta_status_by_id_on_db(
                db,
                brainstorm_request_id=run_id,
                status=BrainstormRequestStatus.FAILURE,
            )

            await db.commit()
