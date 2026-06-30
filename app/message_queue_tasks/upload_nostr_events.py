from datetime import timedelta
from app.core.database import db_session
from app.core.loggr import loggr
from app.core.vespa import batch_upsert_scores
from app.db_models import BrainstormRequestStatus
from app.models.grapeRankResult import GrapeRankResult
from app.repos.brainstorm_nsec import (
    get_last_published_pubkeys_by_pubkey_on_db,
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
    set_is_observer_search_available_by_pubkey_on_db,
    update_last_published_pubkeys_by_pubkey_on_db,
)
from app.repos.brainstorm_request_repo import (
    select_brainstorm_request_by_id_on_db,
    update_brainstorm_request_status_by_id_on_db,
    update_brainstorm_request_ta_status_by_id_on_db,
)
from nostr_sdk import (  # type: ignore
    Client,
    ClientMessage,
    Event,
    EventBuilder,
    EventId,
    Filter,
    Keys,
    Kind,
    NostrSigner,
    PublicKey,
    Tag,
)
import time
from contextlib import contextmanager
from app.core.config import settings

logger = loggr.get_logger(__name__)

RELAYS: list[str] = [
    x
    for x in [
        settings.nostr_upload_ta_events_relay,
        # settings.nostr_transfer_to_relay2,
    ]
    if x
]


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
    run_id, observer: str, timings: dict[str, float], counts: dict[str, int],
    run_start: float, error: str | None = None,
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
        except:
            logger.error(f"Bad relay {relay}")
    if relay_count == 0:
        raise Exception("No good relay available, shutting down!")

    logger.info("Finished adding relays!")
    result = await client.try_connect(timedelta(seconds=10))
    assert not bool(result.failed)
    logger.info("Nostr Client Connected!!!")

    return client


async def get_events_from_graperank_result(
    grape_rank_result: GrapeRankResult, nostr_client: Client
) -> list[Event]:

    events: list[Event] = []
    logger.info(f"{bool(grape_rank_result.scorecards)}")
    assert grape_rank_result.scorecards is not None
    changed_pubkeys = set(grape_rank_result.changedScorePubkeys)
    # relay_full_sync=True republishes every above-cutoff TA (reconciliation /
    # drift correction); False publishes only changed scores (steady state).
    logger.info(
        f"relay_full_sync={settings.relay_full_sync}: publishing "
        f"{'all above-cutoff' if settings.relay_full_sync else f'{len(changed_pubkeys)} changed-score'} "
        f"pubkeys out of {len(grape_rank_result.scorecards)} scorecards"
    )
    start_time_sort = time.time()
    logger.info("sorting scorecards...")
    sorted_scorecards = sorted(
        (
            sc
            for pubkey, sc in grape_rank_result.scorecards.items()
            if settings.relay_full_sync or pubkey in changed_pubkeys
        ),
        key=lambda sc: sc.influence,
        reverse=True,
    )
    end_time_sort = time.time() - start_time_sort
    logger.info(f"sorted scorecards! took {round(end_time_sort,2)}s")

    for scorecard in sorted_scorecards:

        if round(scorecard.influence, 2) < settings.cutoff_of_valid_graperank_scores:
            continue

        d_tag = scorecard.observee

        rank_tag = round(scorecard.influence * 100)

        trusted_followers_count = scorecard.trusted_followers

        tags = [
            Tag.parse(["d", d_tag]),
            Tag.parse(["rank", str(rank_tag)]),
            Tag.parse(["followers", str(trusted_followers_count)]),
        ]

        event_builder = EventBuilder(
            kind=Kind(30382),
            content="",
        )

        event_builder = event_builder.tags(tags)

        signed_event = await nostr_client.sign_event_builder(event_builder)

        events.append(signed_event)
    logger.info(f"publishing change results. total number: {len(events)} ")
    return events


async def get_zero_score_events_for_pubkeys(
    pubkeys: list[str],
    nostr_client: Client,
) -> list[Event]:
    # Replaceable kind 30382 events with rank=0. Published before the kind 5
    # deletions so that even if the relay rejects kind 5, the score is
    # effectively zeroed out.
    events: list[Event] = []
    for pk in pubkeys:
        tags = [
            Tag.parse(["d", pk]),
            Tag.parse(["rank", "0"]),
            Tag.parse(["followers", "0"]),
        ]
        builder = EventBuilder(kind=Kind(30382), content="").tags(tags)
        signed_event = await nostr_client.sign_event_builder(builder)
        events.append(signed_event)
    return events


DELETION_FETCH_BATCH_SIZE = 200


async def fetch_existing_events_for_dropped_pubkeys(
    author_pubkey: str,
    dropped_pubkeys: list[str],
) -> list[Event]:

    fetcher = Client()
    added = 0
    for relay in RELAYS:
        try:
            await fetcher.add_relay(relay)
            added += 1
        except Exception as e:
            logger.error(f"deletion fetch: bad relay {relay}: {e}")
    if added == 0:
        logger.error("deletion fetch: no relays available, skipping")
        return []

    await fetcher.connect()
    try:
        author = PublicKey.parse(author_pubkey)
        all_events: list[Event] = []
        seen_ids: set[str] = set()
        total_batches = (
            len(dropped_pubkeys) + DELETION_FETCH_BATCH_SIZE - 1
        ) // DELETION_FETCH_BATCH_SIZE
        for i in range(0, len(dropped_pubkeys), DELETION_FETCH_BATCH_SIZE):
            batch = dropped_pubkeys[i : i + DELETION_FETCH_BATCH_SIZE]
            batch_index = i // DELETION_FETCH_BATCH_SIZE + 1
            flt = Filter().kinds([Kind(30382)]).authors([author]).identifiers(batch)
            batch_start = time.perf_counter()
            try:
                events_obj = await fetcher.fetch_events(
                    flt, timeout=timedelta(seconds=30)
                )
            except Exception as e:
                logger.error(f"deletion fetch batch {batch_index} failed: {e}")
                continue
            # per-batch latency is the direct prod-RTT signal for the sweep
            logger.info(
                f"deletion fetch batch {batch_index}/{total_batches} "
                f"({len(batch)} identifiers) took "
                f"{round((time.perf_counter() - batch_start) * 1000)}ms"
            )
            for ev in events_obj.to_vec():
                eid = ev.id().to_hex()
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                all_events.append(ev)
        return all_events
    finally:
        try:
            await fetcher.disconnect()
        except Exception as e:
            logger.error(f"deletion fetch: disconnect failed: {e}")


async def get_deletion_events_for_dropped_pubkeys(
    author_pubkey: str,
    dropped_pubkeys: list[str],
    nostr_client: Client,
) -> list[Event]:

    if not dropped_pubkeys:
        logger.info(
            f"zero pubkeys that moved below the threshold. no events will be deleted"
        )
        return []

    logger.info(
        f"fetching existing kind 30382 events for {len(dropped_pubkeys)} "
        f"dropped pubkeys to build deletion events"
    )

    existing_events = await fetch_existing_events_for_dropped_pubkeys(
        author_pubkey=author_pubkey,
        dropped_pubkeys=dropped_pubkeys,
    )
    logger.info(f"found {len(existing_events)} existing kind 30382 events to delete")

    event_ids_by_d_tag: dict[str, list[EventId]] = {}
    for ev in existing_events:
        d_tag: str | None = None
        for tag in ev.tags().to_vec():
            tag_vec = tag.as_vec()
            if len(tag_vec) >= 2 and tag_vec[0] == "d":
                d_tag = tag_vec[1]
                break
        if d_tag is None:
            continue
        event_ids_by_d_tag.setdefault(d_tag, []).append(ev.id())

    deletion_events: list[Event] = []
    for d_tag, event_ids in event_ids_by_d_tag.items():
        tags = [Tag.parse(["e", eid.to_hex()]) for eid in event_ids]
        builder = EventBuilder(kind=Kind(5), content="dropped below cutoff")
        builder = builder.tags(tags)
        signed_event = await nostr_client.sign_event_builder(builder)
        deletion_events.append(signed_event)

    return deletion_events


async def upsert_scores_to_vespa(
    grape_rank_result: GrapeRankResult,
    observer: str,
    pubkeys_to_delete: list[str],
):
    # Each score depends on the observer; the rank lives in a single cell of
    # the `quality_scores` sparse tensor, keyed by the observer pubkey.
    # Vespa partial updates (create=true) create the doc if absent, so unknown
    # pubkeys get a doc carrying just the tensor cell; the kind-0 upsert fills
    # in profile fields later. All ops are fanned out concurrently so a large
    # GrapeRank result completes in seconds rather than minutes.
    assert grape_rank_result.scorecards is not None
    changed_pubkeys = set(grape_rank_result.changedScorePubkeys)

    upserts: list[tuple[str, int]] = []
    for pubkey, scorecard in grape_rank_result.scorecards.items():
        if not settings.vespa_full_sync and pubkey not in changed_pubkeys:
            continue
        if round(scorecard.influence, 2) < settings.cutoff_of_valid_graperank_scores:
            continue
        upserts.append((pubkey, round(scorecard.influence * 100)))

    n_ok, n_failed = await batch_upsert_scores(
        upserts=upserts,
        removes=list(pubkeys_to_delete),
        observer=observer,
    )
    logger.info(
        f"vespa score batch: ok={n_ok} failed={n_failed} "
        f"(upserts={len(upserts)} removes={len(pubkeys_to_delete)})"
    )


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
                nsec_db_obj, _was_created_now = (
                    await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
                        db, pubkey=observer
                    )
                )
            assert nsec_db_obj.pubkey == observer
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
                grape_rank_result, nostr_client
            )
        counts["n_signed"] = len(nostr_events)

        with _timed(timings, "last_published"):
            async with db_session() as db:
                previously_published_pubkeys = (
                    await get_last_published_pubkeys_by_pubkey_on_db(db, pubkey=observer)
                )

        with _timed(timings, "compute_deletes"):
            currently_published_pubkeys = [
                sc.observee
                for sc in grape_rank_result.scorecards.values()
                if round(sc.influence, 2) >= settings.cutoff_of_valid_graperank_scores
            ]

            # Full-sync (per sink) sweeps ALL below-cutoff pubkeys for deletion
            # (reconciliation); incremental deletes only this run's reported drops.
            below_cutoff = [
                sc.observee
                for sc in grape_rank_result.scorecards.values()
                if round(sc.influence, 2) < settings.cutoff_of_valid_graperank_scores
            ]
            dropped = list(grape_rank_result.droppedBelowCutoffPubkeys)

            # Previously-published pubkeys no longer in the scorecards are genuine
            # removals — always deleted from both sinks regardless of full/incremental.
            scorecard_pubkeys = set(grape_rank_result.scorecards.keys())
            missing_from_scorecards = [
                pk for pk in previously_published_pubkeys if pk not in scorecard_pubkeys
            ]
            if missing_from_scorecards:
                logger.info(
                    f"adding {len(missing_from_scorecards)} previously-published pubkeys "
                    f"that are no longer in scorecards to both deletion sets"
                )

            # When both sinks use the same mode (the common case) the delete sets
            # are identical — share one list instead of materialising a second
            # ~N-element copy. They differ only when reconciling one sink alone.
            if settings.relay_full_sync == settings.vespa_full_sync:
                base = below_cutoff if settings.relay_full_sync else dropped
                relay_pubkeys_to_delete = vespa_pubkeys_to_delete = (
                    base + missing_from_scorecards
                )
            else:
                relay_pubkeys_to_delete = (
                    below_cutoff if settings.relay_full_sync else dropped
                ) + missing_from_scorecards
                vespa_pubkeys_to_delete = (
                    below_cutoff if settings.vespa_full_sync else dropped
                ) + missing_from_scorecards
        counts["n_above_cutoff"] = len(currently_published_pubkeys)
        counts["n_relay_deletes"] = len(relay_pubkeys_to_delete)
        counts["n_vespa_deletes"] = len(vespa_pubkeys_to_delete)

        # zero_score_events = await get_zero_score_events_for_pubkeys(
        #     pubkeys=pubkeys_to_delete,
        #     nostr_client=nostr_client,
        # )
        # nostr_events.extend(zero_score_events)

        with _timed(timings, "deletion_fetch"):
            deletion_events = await get_deletion_events_for_dropped_pubkeys(
                author_pubkey=signing_pubkey,
                dropped_pubkeys=relay_pubkeys_to_delete,
                nostr_client=nostr_client,
            )
        counts["n_deletion_events"] = len(deletion_events)

        nostr_events.extend(deletion_events)

        with _timed(timings, "send"):
            write_relays = list((await nostr_client.relays()).values())
            for index, nostr_event in enumerate(nostr_events):
                if index == 0 or index % 200 == 0:
                    logger.info(
                        f"still sending nostr events for observer {observer}, progress: {index}"
                    )
                msg = ClientMessage.event(nostr_event)
                for relay in write_relays:
                    try:
                        relay.send_msg(msg)
                    except Exception as e:
                        logger.error(
                            f"Failed to enqueue event {index} on {relay.url()}: {e}"
                        )

        vespa_search_available = False
        try:
            with _timed(timings, "vespa"):
                logger.info(f"Pushing scores to Vespa...")
                await upsert_scores_to_vespa(
                    grape_rank_result=grape_rank_result,
                    observer=observer,
                    pubkeys_to_delete=vespa_pubkeys_to_delete,
                )
            vespa_search_available = True
            logger.info(f"Done pushing scores to Vespa!")
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

                await update_last_published_pubkeys_by_pubkey_on_db(
                    db,
                    pubkey=observer,
                    published_pubkeys=currently_published_pubkeys,
                    graperank_request_id=run_id,
                )

                if vespa_search_available:
                    await set_is_observer_search_available_by_pubkey_on_db(
                        db, pubkey=observer, is_available=True
                    )

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
