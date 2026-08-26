import asyncio
import time
from contextlib import contextmanager
from datetime import timedelta
from typing import NamedTuple

from nostr_sdk import Client, ClientMessage, Event, Keys, NostrSigner  # type: ignore

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.core.vespa import batch_upsert_scores
from app.db_models import BrainstormRequestStatus
from app.message_queue_tasks.ta_signing import (
    UNREACHABLE_HOPS,
    TaInput,
    build_atag_deletion_builders,
    build_ta_event_builder,
    sign_ta_events_parallel,
)
from app.models.grapeRankResult import GrapeRankResult
from app.repos.brainstorm_nsec import (
    get_last_published_pubkeys_by_pubkey_on_db,
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
    reset_runs_since_full_on_db,
    set_is_observer_search_available_by_pubkey_on_db,
    update_last_published_pubkeys_by_pubkey_on_db,
    update_last_time_published_graperank_on_db,
)
from app.repos.brainstorm_request_repo import (
    select_brainstorm_request_by_id_on_db,
    update_brainstorm_request_publish_duration_by_id_on_db,
    update_brainstorm_request_ta_status_by_id_on_db,
)
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
    assert grape_rank_result.scorecards is not None
    changed_pubkeys = set(grape_rank_result.changedScorePubkeys)
    selected = sorted(
        (
            sc
            for pubkey, sc in grape_rank_result.scorecards.items()
            if full_sync or pubkey in changed_pubkeys
        ),
        key=lambda sc: sc.influence,
        reverse=True,
    )
    return [
        TaInput(
            sc.observee,
            round(sc.influence * 100),
            sc.trusted_followers,
            sc.trusted_reporters,
            sc.trusted_muters,
            sc.hops,
        )
        for sc in selected
        if round(sc.influence, 2) >= cutoff
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
    """One sink's delete set — computed from a local diff, no relay read.

    Default: `previously_published - currently_published` (everything we'd
    published that is no longer above cutoff; this subsumes both genuine removals
    and below-cutoff drops, including ones the algo never reported).

    `sweep_below_cutoff` additionally deletes every below-cutoff Observee, reaping
    orphans the diff can't see (legacy sub-cutoff rows, best-effort-write
    leftovers). Nearly all of those were never published, so the sweep is mostly
    no-op deletes that still cost a kind-5 tombstone (relay) / a remove op
    (Vespa) — hence its own gate (`settings.*_sweep_below_cutoff`), NOT tied to
    full-sync: full-sync re-asserts, it does not decide what to delete."""
    return _delete_from_sets(
        set(previously_published) - set(currently_published),
        set(below_cutoff),
        sweep_below_cutoff,
    )


class PublishPlan(NamedTuple):
    """What one publish run does with an algo result, decided up front.

    `currently_published` is every above-cutoff Observee (the new published-state
    to persist). `relay_deletes` / `vespa_deletes` are each sink's delete set:
    equal whenever both sinks sweep the same way, but genuinely independent,
    because the sinks pay different prices for a sweep (a Vespa remove is a cheap
    idempotent op; a relay delete costs a kind-5 tombstone) and an admin resync
    can target one sink alone."""

    currently_published: list[str]
    relay_deletes: list[str]
    vespa_deletes: list[str]


def plan_publish(
    grape_rank_result: GrapeRankResult,
    previously_published: list[str],
    cutoff: float,
    sweep_relay: bool = False,
    sweep_vespa: bool = False,
) -> PublishPlan:
    """Decide the per-sink delete sets + the new published-state from one algo
    result, with no relay read. Pure, so the whole publish decision is testable
    against algo results without DB/relay/signing.

    Partitions scorecards into above/below cutoff in a single pass, builds each
    set once, and reuses them across both sinks — when the two sweep flags match
    (the common case) the delete set is computed once and shared.

    The delete sets do NOT depend on the full-sync flags — those decide what each
    sink re-asserts, not what it deletes (see `compute_delete_observees`)."""
    assert grape_rank_result.scorecards is not None
    currently_published: list[str] = []
    above: set[str] = set()
    below: set[str] = set()
    for sc in grape_rank_result.scorecards.values():
        if round(sc.influence, 2) >= cutoff:
            currently_published.append(sc.observee)
            above.add(sc.observee)
        else:
            below.add(sc.observee)

    fell_off = set(previously_published) - above
    relay_deletes = _delete_from_sets(fell_off, below, sweep_relay)
    vespa_deletes = (
        relay_deletes
        if sweep_relay == sweep_vespa
        else _delete_from_sets(fell_off, below, sweep_vespa)
    )
    return PublishPlan(currently_published, relay_deletes, vespa_deletes)


def published_state_to_persist(
    previously_published: list[str],
    currently_published: list[str],
    sink_write_failed: bool,
) -> list[str]:
    """The `last_published_pubkeys` baseline to persist after a run.

    Clean run → exactly the intended above-cutoff set. But if any sink write
    failed this run, a delete may not have landed, and pruning the baseline past
    it would orphan that pubkey in the sink forever (no future `fell_off` could
    ever see it). So on a dirty run keep prev + current: the un-confirmed delete
    stays in the baseline and is re-issued (idempotently) next run, while newly
    above-cutoff keys are still tracked. A later clean run prunes back to
    `currently_published`, so the baseline self-corrects and can't grow unbounded.

    Coarse on purpose — it keys on "any sink failure", not on which delete
    failed, so it over-retains slightly (a redundant, harmless re-delete). This
    only prevents NEW orphans; orphans already in the sinks need the reset sweep."""
    if sink_write_failed:
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

# `relay.send_msg` only *enqueues* onto nostr-sdk's bounded per-relay channel; it
# raises ("can't send message to the ... channel") when that channel is transiently
# full while the SDK's writer drains the socket. Under a flood (~24k events in <1s)
# this surfaces as short bursts of failures. Retry with a small backoff so the
# writer can catch up; `await asyncio.sleep` yields the loop so the channel drains.
SEND_MSG_MAX_ATTEMPTS = 5
SEND_MSG_RETRY_BASE_DELAY_S = 0.01


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
    counts: dict[str, int],
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


async def _sign_ta_events_sequential(
    inputs: list[TaInput],
    nostr_client: Client,
) -> list[Event]:
    """Sign each TA via the relay-connected client, one await at a time."""
    events: list[Event] = []
    for ta_input in inputs:
        builder = build_ta_event_builder(ta_input)
        events.append(await nostr_client.sign_event_builder(builder))
    return events


async def get_events_from_graperank_result(
    grape_rank_result: GrapeRankResult,
    nostr_client: Client,
    nsec: str,
    relay_full_sync: bool | None = None,
) -> list[Event]:
    assert grape_rank_result.scorecards is not None
    # Resolved per-run mode (settings default OR a per-run force_full override);
    # callers without an override pass None and get the env default.
    if relay_full_sync is None:
        relay_full_sync = settings.relay_full_sync
    changed_pubkeys = set(grape_rank_result.changedScorePubkeys)
    # relay_full_sync=True republishes every above-cutoff TA (reconciliation /
    # drift correction); False publishes only changed scores (steady state).
    logger.info(
        f"relay_full_sync={relay_full_sync}: publishing "
        f"{'all above-cutoff' if relay_full_sync else f'{len(changed_pubkeys)} changed-score'} "
        f"pubkeys out of {len(grape_rank_result.scorecards)} scorecards"
    )
    start_time_sort = time.time()
    logger.info("sorting scorecards...")
    inputs = prepare_ta_inputs(
        grape_rank_result,
        cutoff=settings.cutoff_of_valid_graperank_scores,
        full_sync=relay_full_sync,
    )
    logger.info(f"sorted scorecards! took {round(time.time() - start_time_sort, 2)}s")

    # Count-gated signing: small (common) runs keep the simple sequential client
    # loop; only a large burst pays for the process pool. Offloading the large
    # sign to child processes both ~10×-speeds it and keeps the GIL-holding
    # nostr-sdk signing off the event loop so concurrent requests aren't starved.
    if len(inputs) <= settings.sign_parallel_threshold:
        events = await _sign_ta_events_sequential(inputs, nostr_client)
    else:
        logger.info(f"large sign ({len(inputs)} events): parallel process pool")
        events = await sign_ta_events_parallel(
            inputs, nsec, max_workers=settings.sign_parallel_max_workers
        )
    logger.info(f"publishing change results. total number: {len(events)} ")
    return events


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


async def upsert_scores_to_vespa(
    grape_rank_result: GrapeRankResult,
    observer: str,
    pubkeys_to_delete: list[str],
    vespa_full_sync: bool,
) -> int:
    # Each score depends on the observer; the rank lives in a single cell of
    # the `quality_scores` sparse tensor, keyed by the observer pubkey.
    # Vespa partial updates (create=true) create the doc if absent, so unknown
    # pubkeys get a doc carrying just the tensor cell; the kind-0 upsert fills
    # in profile fields later. All ops are fanned out concurrently so a large
    # GrapeRank result completes in seconds rather than minutes.
    assert grape_rank_result.scorecards is not None
    changed_pubkeys = set(grape_rank_result.changedScorePubkeys)

    # Vespa now mirrors the relay set exactly: index a score if it's above the
    # publish `cutoff` (same boundary as the Nostr TA events). Below-cutoff
    # scorecards are NOT indexed — their removal is handled by `pubkeys_to_delete`
    # (the previously-published diff / full-sync below-cutoff sweep from
    # `plan_publish`), so there is no separate rank>0 sweep here. Text search still
    # finds below-cutoff profiles via their kind-0 doc (onlyRanked=false); only the
    # trust cell is gated by the cutoff.
    cutoff = settings.cutoff_of_valid_graperank_scores
    upserts: list[tuple[str, int, int]] = []
    for pubkey, scorecard in grape_rank_result.scorecards.items():
        if round(scorecard.influence, 2) < cutoff:
            continue
        if vespa_full_sync or pubkey in changed_pubkeys:
            upserts.append(
                (
                    pubkey,
                    round(scorecard.influence * 100),
                    round(scorecard.trusted_followers),
                )
            )

    n_ok, n_failed = await batch_upsert_scores(
        upserts=upserts,
        removes=pubkeys_to_delete,
        observer=observer,
    )
    logger.info(
        f"vespa score batch: ok={n_ok} failed={n_failed} "
        f"(upserts={len(upserts)} removes={len(pubkeys_to_delete)})"
    )
    # Surfaced so the caller can tell whether a remove may not have landed and
    # keep the published-state baseline honest (see published_state_to_persist).
    return n_failed


async def process_nostr_upload_message(message: dict):
    # is_success = message["result"]["success"]

    # if not is_success:
    #     return

    grape_rank_result = GrapeRankResult.model_validate(message["result"])
    if not grape_rank_result.scorecards:
        return
    observer = next(iter(grape_rank_result.scorecards.values())).observer
    run_id = message["private_id"]
    timings: dict[str, float] = {}
    counts: dict[str, int] = {"n_scorecards": len(grape_rank_result.scorecards)}
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
            # Read this run's per-sink force-full overrides off the request row
            # (drift repair) — folded into the existing status-update session, so
            # no extra round-trip. settings default OR the per-run override.
            request_row = await select_brainstorm_request_by_id_on_db(db, run_id)
            relay_full_sync = resolve_full_sync(
                settings.relay_full_sync, request_row.force_full_relay
            )
            vespa_full_sync = resolve_full_sync(
                settings.vespa_full_sync, request_row.force_full_vespa
            )
            with _timed(timings, "nsec_ta_update"):
                await update_brainstorm_request_ta_status_by_id_on_db(
                    db,
                    brainstorm_request_id=run_id,
                    status=BrainstormRequestStatus.ONGOING,
                )
            with _timed(timings, "nsec_commit"):
                await db.commit()

    try:
        with _timed(timings, "connect"):
            nostr_client: Client = await init_nostr_client(nsec_db_obj.nsec)
        signing_pubkey = Keys.parse(secret_key=nsec_db_obj.nsec).public_key().to_hex()

        with _timed(timings, "sign"):
            nostr_events = await get_events_from_graperank_result(
                grape_rank_result,
                nostr_client,
                nsec_db_obj.nsec,
                relay_full_sync=relay_full_sync,
            )
        counts["n_signed"] = len(nostr_events)

        with _timed(timings, "last_published"):
            async with db_session() as db:
                previously_published_pubkeys = (
                    await get_last_published_pubkeys_by_pubkey_on_db(
                        db, pubkey=observer
                    )
                )

        with _timed(timings, "compute_deletes"):
            # One pass partitions scorecards into above/below cutoff and decides
            # both sinks' fetch-free local-diff delete sets (shared when the two
            # sweep modes match). The local diff subsumes the old dropped+missing
            # set and catches drops the algo never reported. Independent of
            # full-sync: that re-asserts, it does not delete.
            plan = plan_publish(
                grape_rank_result,
                previously_published=previously_published_pubkeys,
                cutoff=settings.cutoff_of_valid_graperank_scores,
                sweep_relay=settings.relay_sweep_below_cutoff,
                sweep_vespa=settings.vespa_sweep_below_cutoff,
            )
            currently_published_pubkeys = plan.currently_published
            relay_pubkeys_to_delete = plan.relay_deletes
            vespa_pubkeys_to_delete = plan.vespa_deletes
        # A sweep is a backlog-draining mode, not a steady state — say so loudly
        # so it can't sit on unnoticed, billing no-op deletes every run.
        if settings.relay_sweep_below_cutoff or settings.vespa_sweep_below_cutoff:
            logger.warning(
                f"below-cutoff sweep ENABLED for observer {observer} "
                f"(relay={settings.relay_sweep_below_cutoff} "
                f"vespa={settings.vespa_sweep_below_cutoff}): every run reaps every "
                f"below-cutoff Observee. Turn *_SWEEP_BELOW_CUTOFF back off once the "
                f"orphan backlog has drained."
            )
        counts["n_above_cutoff"] = len(currently_published_pubkeys)
        counts["n_relay_deletes"] = len(relay_pubkeys_to_delete)
        counts["n_vespa_deletes"] = len(vespa_pubkeys_to_delete)

        # zero_score_events = await get_zero_score_events_for_pubkeys(
        #     pubkeys=pubkeys_to_delete,
        #     nostr_client=nostr_client,
        # )
        # nostr_events.extend(zero_score_events)

        with _timed(timings, "deletion_build"):
            deletion_events = await get_deletion_events_for_dropped_pubkeys(
                observees=relay_pubkeys_to_delete,
                signing_pubkey=signing_pubkey,
                nostr_client=nostr_client,
            )
        counts["n_deletion_events"] = len(deletion_events)

        nostr_events.extend(deletion_events)

        with _timed(timings, "send"):
            write_relays = list((await nostr_client.relays()).values())
            send_failures = 0
            for index, nostr_event in enumerate(nostr_events):
                if index == 0 or index % 200 == 0:
                    logger.info(
                        f"still sending nostr events for observer {observer}, progress: {index}"
                    )
                msg = ClientMessage.event(nostr_event)
                for relay in write_relays:
                    for attempt in range(SEND_MSG_MAX_ATTEMPTS):
                        try:
                            relay.send_msg(msg)
                            break
                        except Exception as e:
                            if attempt + 1 >= SEND_MSG_MAX_ATTEMPTS:
                                send_failures += 1
                                logger.error(
                                    f"Failed to enqueue event {index} on {relay.url()} "
                                    f"after {SEND_MSG_MAX_ATTEMPTS} attempts: {e}"
                                )
                            else:
                                await asyncio.sleep(
                                    SEND_MSG_RETRY_BASE_DELAY_S * (2**attempt)
                                )
            if send_failures:
                logger.error(
                    f"TA publish: {send_failures} events failed to enqueue after "
                    f"{SEND_MSG_MAX_ATTEMPTS} attempts (run={run_id} observer={observer})"
                )

        vespa_search_available = False
        vespa_n_failed = 0
        try:
            with _timed(timings, "vespa"):
                logger.info("Pushing scores to Vespa...")
                vespa_n_failed = await upsert_scores_to_vespa(
                    grape_rank_result=grape_rank_result,
                    observer=observer,
                    pubkeys_to_delete=vespa_pubkeys_to_delete,
                    vespa_full_sync=vespa_full_sync,
                )
            vespa_search_available = True
            logger.info("Done pushing scores to Vespa!")
        except Exception as e:
            # Don't fail the whole request — Nostr is the source of truth
            # and has already been written. Vespa is a search-side mirror.
            logger.error(f"Failed to upsert scores to Vespa: {e}")

        with _timed(timings, "final_db"):
            async with db_session() as db:
                await update_brainstorm_request_ta_status_by_id_on_db(
                    db,
                    brainstorm_request_id=run_id,
                    status=BrainstormRequestStatus.SUCCESS,
                )

                # Keep the baseline honest: if any sink write may have failed
                # (a total Vespa error, a partial Vespa remove failure, or a
                # relay enqueue failure), a delete may not have landed — don't
                # prune those pubkeys out of the baseline or they orphan in the
                # sink forever. Retain prev ∪ current so the delete is retried.
                sink_write_failed = (
                    send_failures > 0
                    or vespa_n_failed > 0
                    or not vespa_search_available
                )
                await update_last_published_pubkeys_by_pubkey_on_db(
                    db,
                    pubkey=observer,
                    published_pubkeys=published_state_to_persist(
                        previously_published=previously_published_pubkeys,
                        currently_published=currently_published_pubkeys,
                        sink_write_failed=sink_write_failed,
                    ),
                    graperank_request_id=run_id,
                )

                # Freshness clock for the scheduler — advanced only on success.
                await update_last_time_published_graperank_on_db(db, pubkey=observer)

                # Measured publish duration for the scheduler's throughput metrics.
                await update_brainstorm_request_publish_duration_by_id_on_db(
                    db, run_id, round(time.perf_counter() - run_start, 3)
                )

                if vespa_search_available:
                    await set_is_observer_search_available_by_pubkey_on_db(
                        db, pubkey=observer, is_available=True
                    )

                # A successful full run on BOTH sinks brings the observer fully in
                # sync → clear the every-Nth backstop counter (any full run counts,
                # incl. an admin resync). Piggybacks on this existing Nsec write.
                if relay_full_sync and vespa_full_sync:
                    await reset_runs_since_full_on_db(db, pubkey=observer)

                await db.commit()

        _log_publish_timing(run_id, observer, timings, counts, run_start)
        if nostr_events:
            logger.info(f"Check Nostr Event {nostr_events[0].as_json()}")
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
