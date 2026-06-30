"""Admin reconcile orchestrator: diff a single observer's actual published state
against the live Neo4j desired state and (optionally) repair only the deltas.

Wires the pure classifiers in `reconcile.py` to I/O — the Neo4j desired read, the
relay enumeration, the Vespa GET-loop, and targeted apply via the existing
publish + vespa primitives. Per-segment wall-clock is recorded with the same
`timed` helper the publish run uses and returned in the report.
"""

import asyncio

from nostr_sdk import (  # type: ignore
    Client,
    ClientMessage,
    Filter,
    HandleNotification,
    Keys,
    Kind,
    PublicKey,
    RelayMessage,
)

from app.core import vespa
from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.message_queue_tasks.ta_signing import (
    TA_KIND,
    build_atag_deletion_builders,
    build_ta_event_builder,
)
from app.message_queue_tasks.upload_nostr_events import init_nostr_client
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.brainstorm_nsec import (
    get_last_published_pubkeys_by_pubkey_on_db,
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
    update_last_published_pubkeys_blob_on_db,
)
from app.repos.user_repo import get_all_observer_influence
from app.services.reconcile import (
    RelayDriftAccumulator,
    build_desired_map,
    classify_vespa_cell,
    summarize_drift,
)
from app.utils.timing import timed

logger = loggr.get_logger(__name__)

# Reconcile-corrected TAs carry followers=0: the Neo4j desired read provides the
# score (rank) only, and the relay diff compares rank, not the followers tag.
_RECONCILE_FOLLOWERS = 0

_RECONCILE_SUB_ID = "reconcile-drift"
# Cap on how long we wait for the relay's EOSE (end of stored events) before
# giving up on the stream — a relay that never EOSEs shouldn't hang the request.
_STREAM_EOSE_TIMEOUT_S = 60.0


class _DriftStreamHandler(HandleNotification):
    """Feeds each streamed TA into the accumulator (then drops it) and flags EOSE
    so the caller can stop — the relay's actual set is never buffered.

    Stored events from a subscription arrive as raw relay messages
    (`handle_msg`, `EVENT_MSG`), not the higher-level `handle` notification, so
    both extraction and the EOSE signal are handled there."""

    def __init__(self, acc: RelayDriftAccumulator, eose: asyncio.Event) -> None:
        # The client is dedicated to this single reconcile subscription, so every
        # delivered message belongs to it — no need to filter by subscription id.
        self._acc = acc
        self._eose = eose

    async def handle(self, relay_url: str, subscription_id: str, event) -> None:
        pass  # stored events come via handle_msg/EVENT_MSG, not here

    async def handle_msg(self, relay_url: str, msg: RelayMessage) -> None:
        try:
            enum = msg.as_enum()
            if enum.is_event_msg():
                d_tag, rank = _event_d_and_rank(enum.event)
                if d_tag is not None and rank is not None:
                    self._acc.observe(d_tag, rank)
            elif enum.is_end_of_stored_events():
                self._eose.set()
        except Exception as exc:
            logger.error(f"reconcile relay stream: bad message: {exc}")


def _event_d_and_rank(event) -> tuple[str | None, int | None]:
    d_tag: str | None = None
    rank: int | None = None
    for tag in event.tags().to_vec():
        vec = tag.as_vec()
        if len(vec) >= 2 and vec[0] == "d":
            d_tag = vec[1]
        elif len(vec) >= 2 and vec[0] == "rank":
            try:
                rank = int(vec[1])
            except ValueError:
                rank = None
    return d_tag, rank


async def _stream_relay_into(
    acc: RelayDriftAccumulator, client: Client, signer_pubkey: str
) -> None:
    """Subscribe to the observer's TAs and push each into the accumulator as it
    arrives, dropping the event — the actual set is never buffered, so memory
    stays bounded (desired map + corrections) on a 134k-Observee observer.

    Runs `handle_notifications` on a side task, stops on the relay's EOSE (bounded
    by a timeout), then unsubscribes and cancels the task."""
    flt = Filter().kinds([Kind(TA_KIND)]).authors([PublicKey.parse(signer_pubkey)])
    eose = asyncio.Event()
    handler = _DriftStreamHandler(acc, eose)
    # Start the notification handler BEFORE subscribing: its receiver must be live
    # when the relay streams the stored events, or a fast (local) response is lost
    # to the broadcast-channel race.
    task = asyncio.create_task(client.handle_notifications(handler))
    await asyncio.sleep(0.1)
    await client.subscribe_with_id(_RECONCILE_SUB_ID, flt)
    try:
        await asyncio.wait_for(eose.wait(), timeout=_STREAM_EOSE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            f"reconcile relay stream: no EOSE within {_STREAM_EOSE_TIMEOUT_S}s"
        )
    finally:
        try:
            await client.unsubscribe(_RECONCILE_SUB_ID)
        except Exception as exc:
            logger.error(f"reconcile relay: unsubscribe failed: {exc}")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _reconcile_relay(
    desired: dict[str, int], signer_nsec: str, apply: bool, full: bool
) -> dict:
    keys = Keys.parse(secret_key=signer_nsec)
    signer_pubkey = keys.public_key().to_hex()
    client = await init_nostr_client(signer_nsec)
    try:
        acc = RelayDriftAccumulator(desired)
        await _stream_relay_into(acc, client, signer_pubkey)
        drift = acc.result()
        applied = 0
        if apply:
            write_relays = list((await client.relays()).values())
            corrected = drift.missing + drift.stale
            events = [
                build_ta_event_builder(obs, rank, _RECONCILE_FOLLOWERS).sign_with_keys(
                    keys
                )
                for obs, rank in corrected
            ]
            if drift.ghost:
                for builder in build_atag_deletion_builders(drift.ghost, signer_pubkey):
                    events.append(builder.sign_with_keys(keys))
            for event in events:
                msg = ClientMessage.event(event)
                for relay in write_relays:
                    relay.send_msg(msg)
            applied = len(events)
    finally:
        try:
            await client.disconnect()
        except Exception as exc:
            logger.error(f"reconcile relay: disconnect failed: {exc}")

    report = summarize_drift(
        [obs for obs, _ in drift.missing],
        [obs for obs, _ in drift.stale],
        drift.ghost,
        full=full,
    )
    report["applied"] = applied if apply else 0
    return report


async def _reconcile_vespa(
    desired: dict[str, int],
    observer: str,
    last_published: list[str],
    apply: bool,
    full: bool,
) -> dict:
    missing: list[str] = []
    stale: list[str] = []
    for observee, expected in desired.items():
        cell = await vespa.get_observer_score(observee, observer)
        verdict = classify_vespa_cell(expected, cell)
        if verdict == "missing":
            missing.append(observee)
        elif verdict == "stale":
            stale.append(observee)

    # Ghosts: previously-published cells no longer in the desired set.
    ghost: list[str] = []
    for observee in set(last_published) - set(desired):
        if await vespa.get_observer_score(observee, observer) is not None:
            ghost.append(observee)

    applied = 0
    if apply:
        for observee in missing + stale:
            await vespa.upsert_score(observee, observer, desired[observee])
        for observee in ghost:
            await vespa.remove_score(observee, observer)
        applied = len(missing) + len(stale) + len(ghost)

    report = summarize_drift(missing, stale, ghost, full=full)
    report["applied"] = applied if apply else 0
    return report


async def reconcile_observer(
    observer: str, target: str, apply: bool, full: bool
) -> dict:
    """Diff (and optionally repair) one observer's published state per sink.
    Returns a per-sink drift report plus segment timings."""
    timings: dict[str, float] = {}
    report: dict = {"observer": observer, "target": target, "apply": apply}

    with timed(timings, "desired_read"):
        async with neo4j_driver.session() as session:
            rows = await get_all_observer_influence(session, observer)
        desired = build_desired_map(rows, settings.cutoff_of_valid_graperank_scores)
    report["desired_count"] = len(desired)

    async with db_session() as db:
        nsec_obj, _ = await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
            db, pubkey=observer
        )
        last_published = await get_last_published_pubkeys_by_pubkey_on_db(
            db, pubkey=observer
        )

    if target in ("relay", "both"):
        with timed(timings, "relay"):
            report["relay"] = await _reconcile_relay(
                desired, nsec_obj.nsec, apply, full
            )

    if target in ("vespa", "both"):
        with timed(timings, "vespa"):
            report["vespa"] = await _reconcile_vespa(
                desired, observer, last_published, apply, full
            )

    # A successful both-sink apply brings the full published state in line with
    # desired → refresh the baseline the normal publish path diffs against.
    if apply and target == "both":
        with timed(timings, "update_baseline"):
            async with db_session() as db:
                await update_last_published_pubkeys_blob_on_db(
                    db, pubkey=observer, published_pubkeys=list(desired.keys())
                )

    report["timings"] = timings
    logger.info(
        f"reconcile observer={observer} target={target} apply={apply} "
        f"desired={len(desired)} timings={timings}"
    )
    return report
