import asyncio
from datetime import timedelta
from collections import deque

from app.core.loggr import loggr
from nostr_sdk import Client, Filter, Kind, Timestamp, Event
from tqdm import tqdm
import os
from collections import deque
import time
from app.core.database import db_session
from app.core.config import settings
from app.repos.brainstorm_nostr_transferer import (
    get_nostr_transfer_status_by_kind_from_db,
    upsert_nostr_transfer_status_on_db,
)

logger = loggr.get_logger(__name__)

ev_kinds: list[tuple[Kind, int]] = [
    (Kind(3), 1800000),
    (Kind(10000), 1800000),
    (Kind(1984), 1800000),
]


LIMIT = 300
SEEN_CACHE_SIZE = 5000


async def publish_event(event: Event, relay_client: Client) -> None:

    try:
        result = await relay_client.send_event(event)
        if result.failed:
            logger.error(str(result.failed))
            raise Exception("failed to send event")

    except Exception as e:
        logger.error(e)


async def nostr_event_transferer():
    logger.info("nostr_event_transferer")

    relay_fetcher_client = Client()
    await relay_fetcher_client.add_relay(settings.nostr_transfer_from_relay)
    await relay_fetcher_client.connect()

    relay_sender_client = Client()
    await relay_sender_client.add_relay(settings.nostr_transfer_to_relay)
    await relay_sender_client.connect()

    async with db_session() as db:

        for kind, estimated_events in ev_kinds:

            logger.info(f"Getting events of Kind {kind.as_u16()}")

            started_at = time.time()

            until: int | None = None
            total_events = 0

            nostr_kind_status = await get_nostr_transfer_status_by_kind_from_db(
                db, kind=kind.as_u16()
            )

            # progress_bar = tqdm(total=estimated_events)

            if nostr_kind_status:
                if nostr_kind_status.completed:
                    logger.info(
                        f"Already finished transfering Kind {kind.as_u16()} events. Skipping."
                    )
                    continue

                until = nostr_kind_status.oldest
                total_events = nostr_kind_status.events
                # progress_bar.update(nostr_kind_status.events)

            start_time = time.time()

            seen_queue: deque[str] = deque()
            seen_set: set[str] = set()

            next_fetch_task = None

            while True:
                logger.info(
                    f"Progress on Kind {kind.as_u16()}: {total_events}/{estimated_events} { round(total_events / estimated_events, 4)*100}%"
                )
                new_events = 0
                flt = Filter().kinds([kind]).limit(LIMIT)
                if until is not None:
                    flt = flt.until(Timestamp.from_secs(until))

                # initial
                if next_fetch_task is None:
                    next_fetch_task = asyncio.create_task(
                        relay_fetcher_client.fetch_events(
                            flt, timeout=timedelta(seconds=30)
                        )
                    )

                events_obj = await next_fetch_task

                events = events_obj.to_vec()

                # enforce events being lower than the limit date
                if until:
                    events = [x for x in events if x.created_at().as_secs() <= until]

                if not events:
                    assert until
                    logger.warning("Relay returned no events — fully exhausted")
                    await upsert_nostr_transfer_status_on_db(
                        db,
                        kind=kind.as_u16(),
                        completed=True,
                        total_events=total_events,
                        oldest=until,
                        started_at=(
                            nostr_kind_status.started_at
                            if nostr_kind_status
                            else started_at
                        ),
                    )
                    break

                oldest_ts = min((x.created_at().as_secs() for x in events))

                next_flt = Filter().kinds([kind]).limit(LIMIT)
                if until is not None:
                    next_flt = next_flt.until(Timestamp.from_secs(oldest_ts))

                next_fetch_task = asyncio.create_task(
                    relay_fetcher_client.fetch_events(
                        next_flt, timeout=timedelta(seconds=30)
                    )
                )

                for event in events:
                    event_id = event.id().to_hex()
                    if event_id in seen_set:
                        continue

                    seen_queue.append(event_id)
                    seen_set.add(event_id)
                    if len(seen_queue) == seen_queue.maxlen:
                        removed = seen_queue.popleft()
                        seen_set.remove(removed)

                    new_events += 1
                    await publish_event(event, relay_sender_client)

                # Handle same-timestamp edge case
                same_ts_count = sum(
                    1 for e in events if e.created_at().as_secs() == oldest_ts
                )

                if same_ts_count == len(events):
                    until = oldest_ts - 1
                else:
                    until = oldest_ts

                # progress_bar.update(new_events)
                total_events += new_events

                await upsert_nostr_transfer_status_on_db(
                    db,
                    kind=kind.as_u16(),
                    completed=False,
                    total_events=total_events,
                    oldest=until,
                    started_at=(
                        nostr_kind_status.started_at
                        if nostr_kind_status
                        else started_at
                    ),
                )

            total_time = time.time() - start_time
            logger.info(
                f"Took {total_time} seconds for {total_events} events of kind {kind.as_u16()}"
            )
            # print("----")
            # progress_bar.close()
    await relay_fetcher_client.disconnect()
    await relay_sender_client.disconnect()


async def nostr_event_recent_transferer():
    # logger.info("nostr_event_recent_transferer")

    relay_fetcher_client = Client()
    await relay_fetcher_client.add_relay(settings.nostr_transfer_from_relay)
    await relay_fetcher_client.connect()

    relay_sender_client = Client()
    await relay_sender_client.add_relay(settings.nostr_transfer_to_relay)
    await relay_sender_client.connect()

    async with db_session() as db:

        for kind, _ in ev_kinds:

            started_at = time.time()

            status = await get_nostr_transfer_status_by_kind_from_db(
                db, kind=kind.as_u16()
            )

            if not status or not status.started_at:
                logger.info(f"No initial sync info for kind {kind.as_u16()}, skipping.")
                continue

            stop_time = int(status.started_at) - (
                60 * 3
            )  # give it 3min in the past, just to be safe

            logger.info(f"Recent sync Kind {kind.as_u16()} (NOW → {stop_time})")

            until: int | None = None
            seen_queue: deque[str] = deque(maxlen=5000)
            seen_set: set[str] = set()

            next_fetch_task = None
            total_events = 0

            while True:
                logger.info(f"current until: {until}")
                flt = Filter().kinds([kind]).limit(LIMIT)

                if until is not None:
                    flt = flt.until(Timestamp.from_secs(until))

                if next_fetch_task is None:
                    next_fetch_task = asyncio.create_task(
                        relay_fetcher_client.fetch_events(
                            flt, timeout=timedelta(seconds=30)
                        )
                    )

                events_obj = await next_fetch_task
                events = events_obj.to_vec()

                events = [e for e in events if e.created_at().as_secs() >= stop_time]

                if not events:
                    logger.info("Reached initial sync boundary.")
                    await upsert_nostr_transfer_status_on_db(
                        db,
                        kind=kind.as_u16(),
                        completed=True,
                        total_events=total_events,
                        oldest=status.oldest,
                        started_at=started_at,
                    )
                    break

                oldest_ts = min(e.created_at().as_secs() for e in events)

                if until and oldest_ts > until:
                    oldest_ts = until

                next_fetch_task = asyncio.create_task(
                    relay_fetcher_client.fetch_events(
                        Filter()
                        .kinds([kind])
                        .limit(LIMIT)
                        .until(Timestamp.from_secs(oldest_ts)),
                        timeout=timedelta(seconds=30),
                    )
                )

                new_events = 0

                for event in events:
                    event_id = event.id().to_hex()

                    if event_id in seen_set:
                        continue

                    seen_queue.append(event_id)
                    seen_set.add(event_id)

                    if len(seen_queue) == seen_queue.maxlen:
                        removed = seen_queue.popleft()
                        seen_set.remove(removed)

                    await publish_event(event, relay_sender_client)
                    new_events += 1

                total_events += new_events

                # same timestamp edge case
                same_ts_count = sum(
                    1 for e in events if e.created_at().as_secs() == oldest_ts
                )

                if same_ts_count == len(events):
                    new_until = oldest_ts - 1
                else:
                    new_until = oldest_ts

                if until and new_until == until:
                    until -= 1
                else:
                    until = new_until

            logger.info(
                f"Recent sync finished for Kind {kind.as_u16()} "
                f"({total_events} events)"
            )

    await relay_fetcher_client.disconnect()
    await relay_sender_client.disconnect()


async def nostr_event_recent_transferer_cronjob():
    logger.info("Relay sync cronjob started!")
    while True:
        try:
            await nostr_event_recent_transferer()
        except Exception as e:
            logger.error("Relay sync errored! " + str(e))
        logger.info("Relay sync finished! sleeping for 5min")
        await asyncio.sleep(60 * 5)
