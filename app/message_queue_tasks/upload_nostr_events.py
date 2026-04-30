import asyncio
from datetime import timedelta
from app.core.database import db_session
from app.core.loggr import loggr
from app.db_models import BrainstormRequestStatus
from app.models.grapeRankResult import GrapeRankResult
from app.repos.brainstorm_nsec import (
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.repos.brainstorm_request_repo import (
    select_brainstorm_request_by_id_on_db,
    update_brainstorm_request_status_by_id_on_db,
    update_brainstorm_request_ta_status_by_id_on_db,
)
from nostr_sdk import (  # type: ignore
    Client,
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
    logger.info(
        f"filtering to {len(changed_pubkeys)} changed-score pubkeys "
        f"out of {len(grape_rank_result.scorecards)} scorecards"
    )
    start_time_sort = time.time()
    logger.info("sorting scorecards...")
    sorted_scorecards = sorted(
        (
            sc
            for pubkey, sc in grape_rank_result.scorecards.items()
            if pubkey in changed_pubkeys
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


DELETION_FETCH_BATCH_SIZE = 200

# When True, ignore graperank's droppedBelowCutoffPubkeys list and instead
# delete events for every scorecard whose influence is below the cutoff.
# Used as a backwards-compat sweep until older results are cleaned up.
DELETE_ALL_BELOW_CUTOFF_EVENTS = True


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
            logger.info(
                f"deletion fetch batch {batch_index}/{total_batches} "
                f"({len(batch)} identifiers)"
            )
            flt = Filter().kinds([Kind(30382)]).authors([author]).identifiers(batch)
            try:
                events_obj = await fetcher.fetch_events(
                    flt, timeout=timedelta(seconds=30)
                )
            except Exception as e:
                logger.error(f"deletion fetch batch {batch_index} failed: {e}")
                continue
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


async def process_nostr_upload_message(message: dict):

    # is_success = message["result"]["success"]

    # if not is_success:
    #     return

    grape_rank_result = GrapeRankResult.model_validate(message["result"])
    if not grape_rank_result.scorecards:
        return
    observer = next(iter(grape_rank_result.scorecards.values())).observer
    # TODO: generate a new nsec for the observer of the observer
    async with db_session() as db:
        nsec_db_obj, _was_created_now = (
            await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
                db, pubkey=observer
            )
        )
        assert nsec_db_obj.pubkey == observer
        await update_brainstorm_request_ta_status_by_id_on_db(
            db,
            brainstorm_request_id=message["private_id"],
            status=BrainstormRequestStatus.ONGOING,
        )

        await db.commit()

    try:
        nostr_client: Client = await init_nostr_client(nsec_db_obj.nsec)
        signing_pubkey = Keys.parse(secret_key=nsec_db_obj.nsec).public_key().to_hex()

        nostr_events = await get_events_from_graperank_result(
            grape_rank_result, nostr_client
        )

        if DELETE_ALL_BELOW_CUTOFF_EVENTS:
            pubkeys_to_delete = [
                sc.observee
                for sc in grape_rank_result.scorecards.values()
                if round(sc.influence, 2) < settings.cutoff_of_valid_graperank_scores
            ]
            logger.info(
                f"DELETE_ALL_BELOW_CUTOFF_EVENTS=True: sweeping all "
                f"{len(pubkeys_to_delete)} below-cutoff pubkeys "
                f"instead of using droppedBelowCutoffPubkeys"
            )
        else:
            pubkeys_to_delete = grape_rank_result.droppedBelowCutoffPubkeys

        deletion_events = await get_deletion_events_for_dropped_pubkeys(
            author_pubkey=signing_pubkey,
            dropped_pubkeys=pubkeys_to_delete,
            nostr_client=nostr_client,
        )

        nostr_events.extend(deletion_events)

        start_time = time.time()

        # sem = asyncio.Semaphore(5)

        # tasks = [
        #     asyncio.create_task(
        #         send_nostr_event_with_limit(nostr_client, nostr_event, index, sem)
        #     )
        #     for index, nostr_event in enumerate(nostr_events)
        # ]

        # await asyncio.gather(*tasks, return_exceptions=True)

        for index, nostr_event in enumerate(nostr_events):
            if index == 0 or index % 200 == 0:
                logger.info(
                    f"still sending nostr events for observer {observer}, progress: {index}"
                )
            await send_nostr_event_with_limit(nostr_client, nostr_event, index)

        async with db_session() as db:

            await update_brainstorm_request_ta_status_by_id_on_db(
                db,
                brainstorm_request_id=message["private_id"],
                status=BrainstormRequestStatus.SUCCESS,
            )

            await db.commit()

        final_time = round(time.time() - start_time)
        logger.info(
            f"Took {final_time} seconds to process {len(nostr_events)} nostr events"
        )
        if nostr_events:
            logger.info(f"Check Nostr Event {nostr_events[0].as_json()}")
    except Exception as e:
        logger.error(f"Error on request {message["private_id"]} , {e}")
        async with db_session() as db:

            await update_brainstorm_request_ta_status_by_id_on_db(
                db,
                brainstorm_request_id=message["private_id"],
                status=BrainstormRequestStatus.FAILURE,
            )

            await db.commit()


async def send_nostr_event_with_limit(
    nostr_client: Client, nostr_event: Event, index: int  # , sem: asyncio.Semaphore
):
    # async with sem:
    # print(f"sending {index}...")
    # is_connected = [(await nostr_client.relay(x)).is_connected() for x in RELAYS]

    # if not is_connected:
    #     result = await nostr_client.try_connect(timedelta(seconds=10))
    #     assert not bool(result.failed)

    sent_event_output = await nostr_client.send_event(nostr_event)
    if sent_event_output.failed:
        logger.error(f"Failed to publish event: {sent_event_output.failed}")
