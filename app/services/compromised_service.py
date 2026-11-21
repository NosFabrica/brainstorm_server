"""Compromised accounts: every Observer asserts them at rank 0.

An admin adds an account to `compromised_pubkey` (`/admin/compromised`). From
then on no GrapeRank run brings its score back:

- the TA publish re-asserts it at rank 0 and never deletes it
  (`upload_nostr_events.pin_compromised`);
- the Neo4j writer never stores its score and clears it every run;
- the results consumer leaves it out of the Observer's whitelist snapshot.

Those act only when an Observer's next run lands. `zero_compromised` is the
immediate half, with no GrapeRank run: it re-signs every Observer's existing TA
about the account at rank 0, clears its stored scores in Neo4j and drops it
from every whitelist snapshot. Adding runs it; `scripts/zero_compromised_ta.py`
runs it from a pod with per-observer output. Idempotent.

What the account rated counts for nothing: every calc request carries the
marked keys as `compromised_pubkeys` (`scheduler_lanes.enqueue_calc_request`),
and the GrapeRank worker holds them at Influence 0 inside the sweep, a
designation pin included, so their follows, mutes, reports and designations
carry no weight. That applies from each Observer's next run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from nostr_sdk import EventId, Keys, Nip19Event, PublicKey  # type: ignore
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.core.redis_db import redis_client
from app.core.scorecard_database import scorecard_session
from app.db_models import CompromisedPubkey
from app.message_queue_tasks.process_strfry_event import (
    DESIGNATES_KEY_PREFIX,
    FOLLOWED_BY_KEY_PREFIX,
    MUTED_BY_KEY_PREFIX,
    REPORTED_BY_KEY_PREFIX,
)
from app.message_queue_tasks.process_strfry_event import (
    _update_reverse_sets as update_reverse_sets,
)
from app.message_queue_tasks.relay_publisher import publish_events
from app.message_queue_tasks.ta_signing import (
    TA_KIND,
    SignedEvent,
    sign_ta_json,
    zero_ta_input,
)
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.brainstorm_nsec import (
    select_observer_nsecs_on_db,
    select_observer_signers_on_db,
    select_observers_publishing_on_db,
)
from app.repos.compromised_pubkey_repo import (
    delete_compromised_on_db,
    list_compromised_on_db,
    record_zero_report_on_db,
    select_compromised_by_successor_on_db,
    select_compromised_due_on_db,
    select_compromised_in_on_db,
    select_compromised_on_db,
    update_compromised_metadata_on_db,
    upsert_compromised_on_db,
)
from app.repos.observer_whitelist_repo import remove_pubkey_from_whitelists_on_db
from app.repos.scorecard_repo import clear_scores_about_on_db
from app.repos.user_repo import (
    clear_observer_scores,
    delete_outgoing_ratings,
    get_score_keys_on_node,
)
from app.schemas.request_body_schemas import (
    CompromisedMetadataBody,
    CompromisedPubkeyBody,
)
from app.schemas.schemas import CompromisedStatus
from app.services.compromised_authors import forget_compromised_authors
from app.services.designation_service import fetch_latest
from app.services.relationship_sets import set_name
from app.utils.signed_event import SignedEventRejected, verify_signed_event

logger = loggr.get_logger(__name__)

# Signers per relay REQ. Each signer should hold one 30382 per `d`; the REQ asks
# for twice that (under strfry's stock maxFilterLimit, 500), and an answer that
# fills it is treated as truncated: some signer held more than one version.
AUTHORS_PER_REQ = 200
_RELAY_TIMEOUT_SECONDS = 60.0


def parse_pubkey(value: str) -> str:
    """npub or hex → hex. ValueError if neither."""
    try:
        return PublicKey.parse(value.strip()).to_hex()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"{value!r} is not a pubkey") from e


def _without_uri_scheme(value: str) -> str:
    """`nostr:note1…` (NIP-21, as clients copy it) → `note1…`."""
    value = value.strip()
    return value[len("nostr:") :] if value.lower().startswith("nostr:") else value


def parse_event_id(value: str) -> str:
    """hex, note1… or nevent1…, bare or as a `nostr:` URI → hex. ValueError if
    none of those. (nostr-sdk's EventId.parse takes hex and note1… but not
    nevent1….)"""
    value = _without_uri_scheme(value)
    try:
        if value.startswith("nevent1"):
            return Nip19Event.from_bech32(value).event_id().to_hex()
        return EventId.parse(value).to_hex()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"{value!r} is not an event id") from e


@dataclass(frozen=True)
class Observer:
    pubkey: str  # the Observer (brainstorm_nsec.pubkey)
    signer: str  # the key its TAs are signed with (brainstorm_nsec.assistant_pubkey)


@dataclass
class ZeroReport:
    pubkey: str
    # (observer, signer, where its TA was found: "relay", or "db" when the relay
    # couldn't be read and `last_published_pubkeys` decided)
    observers: list[tuple[str, str, str]] = field(default_factory=list)
    relay_read: bool = True  # False: the relay read failed (or was skipped)
    published: int = 0
    acked: int = 0
    complete: bool = True  # every rank-0 TA acked (vacuously true on a dry run)
    neo4j_cleared: int = 0
    whitelists_updated: int = 0
    edges_cut: int = 0

    def summary(self) -> dict:
        """What `last_zero_report` records: the counts, not the observer list."""
        return {
            "observers": len(self.observers),
            "relay_read": self.relay_read,
            "published": self.published,
            "acked": self.acked,
            "complete": self.complete,
            "neo4j_cleared": self.neo4j_cleared,
            "whitelists_updated": self.whitelists_updated,
            "edges_cut": self.edges_cut,
        }


async def sign_zero_tas(
    pubkey: str, signers: list[tuple[Keys, str]]
) -> AsyncIterator[SignedEvent]:
    for keys, signer in signers:
        yield sign_ta_json(zero_ta_input(pubkey), keys, signer)
        await asyncio.sleep(0)


async def fetch_ta_signers(
    url: str, pubkey: str, signers: list[str]
) -> set[str] | None:
    """Which of `signers` the relay holds a 30382 `d=<pubkey>` from. `None` if
    any REQ failed, timed out or may have been truncated. The relay is shared
    across environments, so it is asked only about these signers."""
    import websockets

    found: set[str] = set()
    try:
        async with asyncio.timeout(_RELAY_TIMEOUT_SECONDS):
            async with websockets.connect(url, max_size=None) as ws:
                for n, start in enumerate(range(0, len(signers), AUTHORS_PER_REQ)):
                    batch = signers[start : start + AUTHORS_PER_REQ]
                    sub = f"compromised-{n}"
                    flt = {
                        "kinds": [TA_KIND],
                        "#d": [pubkey],
                        "authors": batch,
                        "limit": 2 * len(batch),
                    }
                    await ws.send(json.dumps(["REQ", sub, flt]))
                    n_events = 0
                    while True:
                        msg = json.loads(await ws.recv())
                        if not isinstance(msg, list) or len(msg) < 2 or msg[1] != sub:
                            continue
                        if msg[0] == "EVENT" and len(msg) >= 3:
                            n_events += 1
                            found.add(msg[2]["pubkey"])
                        elif msg[0] == "EOSE":
                            await ws.send(json.dumps(["CLOSE", sub]))
                            break
                        elif msg[0] == "CLOSED":
                            logger.warning(
                                f"compromised: relay CLOSED {sub}: {msg[2:]}"
                            )
                            return None
                    if n_events >= 2 * len(batch):
                        logger.warning(
                            f"compromised: relay filled the limit ({n_events}) for "
                            f"{len(batch)} signers; the answer may be truncated"
                        )
                        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"compromised: relay read failed: {e!r}")
        return None
    return found


async def load_observers(db: AsyncDBSession, observer: str | None) -> list[Observer]:
    """Every Observer (or one) with its signer, from the stored `assistant_pubkey`;
    an nsec is decrypted only for a row the startup backfill hasn't reached."""
    rows = await select_observer_signers_on_db(db, observer)
    derived: dict[str, str] = {}
    missing = [pk for pk, signer in rows if not signer]
    for pk, nsec in await select_observer_nsecs_on_db(db, missing):
        try:
            derived[pk] = Keys.parse(secret_key=nsec).public_key().to_hex()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"compromised: bad nsec for observer {pk}: {e}")
    return [
        Observer(pk, signer or derived[pk])
        for pk, signer in rows
        if signer or pk in derived
    ]


async def signing_keys(
    db: AsyncDBSession, observers: list[Observer]
) -> list[tuple[Keys, str]]:
    """The keys to sign with, decrypted for these Observers only. Signs with the
    nsec's own pubkey; a stored signer that disagrees is logged."""
    expected = {o.pubkey: o.signer for o in observers}
    out = []
    for pk, nsec in await select_observer_nsecs_on_db(db, list(expected)):
        try:
            keys = Keys.parse(secret_key=nsec)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"compromised: bad nsec for observer {pk}: {e}")
            continue
        signer = keys.public_key().to_hex()
        if signer != expected[pk]:
            logger.warning(
                f"compromised: observer {pk}'s stored signer {expected[pk]} is not "
                f"its nsec's {signer}; signing with the nsec"
            )
        out.append((keys, signer))
    return out


async def cut_outgoing_ratings(pubkey: str) -> int:
    """Delete the key's own follows, mutes, reports and designations, in Neo4j and
    in the Redis copies the worker reads, so GrapeRank neither walks through it
    nor reads what it rated; the ingest records none from it from now on
    (`process_strfry_event`). How many edges went."""
    async with neo4j_driver.session() as session:
        cut = await delete_outgoing_ratings(session, pubkey)
    for kind, prefix in (
        ("FOLLOWS", FOLLOWED_BY_KEY_PREFIX),
        ("MUTES", MUTED_BY_KEY_PREFIX),
        ("REPORTS", REPORTED_BY_KEY_PREFIX),
    ):
        await update_reverse_sets(prefix, pubkey, removed_pubkeys=cut.get(kind))
    name = await set_name(pubkey)
    if name is not None:
        await redis_client.delete(f"{DESIGNATES_KEY_PREFIX}{name}")
    return sum(len(targets) for targets in cut.values())


async def clear_stored_scores(pubkey: str) -> int:
    """Remove every observer's stored score for `pubkey`; how many properties
    (Neo4j) or rows (Scorecard database) went."""
    if settings.scores_in_postgres:
        async with scorecard_session() as db:
            return await clear_scores_about_on_db(db, pubkey)
    async with neo4j_driver.session() as session:
        keys = await get_score_keys_on_node(session, pubkey)
        if keys:
            await clear_observer_scores(session, [pubkey], keys)
    return len(keys)


async def zero_compromised(
    pubkey: str,
    observer: str | None = None,
    apply: bool = True,
    db_only: bool = False,
) -> ZeroReport:
    """Re-sign every Observer's TA about `pubkey` at rank 0, and, with `apply`,
    clear its Neo4j scores and whitelist entries and record the outcome on its
    row (`last_zero_report`). `observer` limits it to one Observer (the TA half
    only, and nothing recorded).

    The relay says which Observers hold a TA. Only when it can't be read (or with
    `db_only`) does `last_published_pubkeys` decide, a scan of every Observer's
    blob that misses TAs the DB lost track of (`relay_read=False`)."""
    relay = settings.nostr_upload_ta_events_relay
    report = ZeroReport(pubkey)
    async with db_session() as db:
        observers = await load_observers(db, observer)
    relay_signers = None
    if not db_only:
        relay_signers = await fetch_ta_signers(
            relay, pubkey, sorted({o.signer for o in observers})
        )
    report.relay_read = relay_signers is not None
    if relay_signers is not None:
        selected = [o for o in observers if o.signer in relay_signers]
        source = "relay"
    else:
        async with db_session() as db:
            published_by = await select_observers_publishing_on_db(db, pubkey, observer)
        selected = [o for o in observers if o.pubkey in published_by]
        source = "db"
    report.observers = [(o.pubkey, o.signer, source) for o in selected]
    logger.info(
        f"compromised {pubkey}: {len(selected)} of {len(observers)} observers hold a "
        f"TA (by {source}) apply={apply}"
    )
    if not apply:
        return report

    if selected:
        async with db_session() as db:
            keys = await signing_keys(db, selected)
        stats = await publish_events(relay, sign_zero_tas(pubkey, keys))
        report.published, report.acked = stats.n_events, stats.n_acked
        report.complete = (
            not stats.aborted and stats.n_unacked == 0 and len(keys) == len(selected)
        )
        if not report.complete:
            logger.error(
                f"compromised {pubkey}: {stats.n_acked}/{len(selected)} rank-0 TAs "
                f"acked: {stats.failure_samples}"
            )
    if observer is None:
        report.edges_cut = await cut_outgoing_ratings(pubkey)
        report.neo4j_cleared = await clear_stored_scores(pubkey)
        async with db_session() as db:
            report.whitelists_updated = await remove_pubkey_from_whitelists_on_db(
                db, pubkey
            )
            await record_zero_report_on_db(db, pubkey, report.summary())
            await db.commit()
    logger.info(
        f"compromised {pubkey}: published {report.published}, acked {report.acked}, "
        f"neo4j props cleared {report.neo4j_cleared}, whitelists "
        f"{report.whitelists_updated}"
    )
    return report


async def zero_compromised_in_background(pubkey: str) -> None:
    """Run a zeroing to the end and never raise; a failure is recorded on the
    row, where `GET /admin/compromised` shows it (and the retry loop sees it)."""
    try:
        await zero_compromised(pubkey)
    except Exception as e:  # noqa: BLE001
        logger.error(f"compromised {pubkey}: zeroing failed: {e!r}")
        try:
            async with db_session() as db:
                await record_zero_report_on_db(db, pubkey, {"error": repr(e)})
                await db.commit()
        except Exception as e2:  # noqa: BLE001
            logger.error(f"compromised {pubkey}: recording the failure failed: {e2!r}")


# Zeroings running in this process, by account: a second request for the same
# account waits on the one under way instead of starting another, and the
# reference keeps the task from being garbage-collected mid-run.
_running: dict[str, asyncio.Task] = {}


async def zero_now(pubkey: str, wait_seconds: float | None) -> bool:
    """Start (or join) the account's zeroing and wait for it, at most
    `wait_seconds` (None: to the end). A run that outlasts the wait keeps going
    and records its outcome when it lands. True when it finished in time."""
    task = _running.get(pubkey)
    if task is None or task.done():
        task = asyncio.create_task(zero_compromised_in_background(pubkey))
        _running[pubkey] = task
        task.add_done_callback(
            lambda t: _running.pop(pubkey, None) if _running.get(pubkey) is t else None
        )
    done, _ = await asyncio.wait({task}, timeout=wait_seconds)
    return task in done


async def retry_due_zeroings(pending_grace: timedelta, retry_after: timedelta) -> int:
    """Re-run the zeroings that never reported or didn't succeed; how many."""
    async with db_session() as db:
        due = await select_compromised_due_on_db(db, pending_grace, retry_after)
    for pubkey in due:
        logger.warning(f"compromised {pubkey}: zeroing due again, re-running")
        await zero_now(pubkey, wait_seconds=None)
    return len(due)


# --- admin ---------------------------------------------------------------------


def _status(row: CompromisedPubkey) -> CompromisedStatus:
    return CompromisedStatus(
        pubkey=row.pubkey,
        compromised=True,
        since=row.compromised_since or row.created_at,
        proof_event_id=row.proof_event_id,
        proof_event=row.proof_event,
        successor_pubkey=row.successor_pubkey,
        successor_event_id=row.successor_event_id,
    )


async def check_compromised(pubkeys: list[str]) -> list[CompromisedStatus]:
    """One status per pubkey (hex), in the order asked: the public read the UI
    uses to flag a compromised account. A key that is a compromised account's
    successor carries that account's record as `replaces`."""
    async with db_session() as db:
        marked = {r.pubkey: r for r in await select_compromised_in_on_db(db, pubkeys)}
        replaced = {
            r.successor_pubkey: r
            for r in await select_compromised_by_successor_on_db(db, pubkeys)
        }
    out = []
    for pk in pubkeys:
        row = marked.get(pk)
        status = (
            _status(row)
            if row is not None
            else CompromisedStatus(pubkey=pk, compromised=False)
        )
        old = replaced.get(pk)
        if old is not None:
            status.replaces = _status(old)
        out.append(status)
    return out


async def get_compromised(pubkey: str) -> CompromisedPubkey | None:
    async with db_session() as db:
        return await select_compromised_on_db(db, pubkey)


async def list_compromised() -> list[CompromisedPubkey]:
    async with db_session() as db:
        return await list_compromised_on_db(db)


def proof_pointer(value: str) -> tuple[str, list[str]]:
    """An event id (hex, note1… or nevent1…, bare or as a `nostr:` URI) → (hex
    id, the nevent's relay hints)."""
    value = _without_uri_scheme(value)
    if value.startswith("nevent1"):
        try:
            pointer = Nip19Event.from_bech32(value)
            return pointer.event_id().to_hex(), [str(r) for r in pointer.relays()]
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"{value!r} is not an event id") from e
    return parse_event_id(value), []


def verified_proof(raw: Any) -> dict:
    """The event as stored: id and signature checked, any kind. 422 otherwise."""
    kind = raw.get("kind") if isinstance(raw, dict) else None
    try:
        event = verify_signed_event(raw, kind=kind if isinstance(kind, int) else -1)
    except SignedEventRejected as e:
        raise HTTPException(
            status_code=422, detail=f"The proof event was rejected: {e.reason.value}."
        )
    return json.loads(event.as_json())


async def fetch_proof_event(event_id: str, hints: list[str]) -> dict:
    """The event with this id, from the nevent's relay hints, the relay we sync
    from, our own and `compromised_proof_relays`, all asked at once: the first
    valid copy wins, so a dead relay costs one fetch timeout, not one each. 422
    when none holds a valid copy."""
    relays = list(
        dict.fromkeys(
            [
                *hints,
                settings.nostr_transfer_from_relay,
                settings.nostr_transfer_to_relay,
                *settings.compromised_proof_relays,
            ]
        )
    )

    async def _from(relay: str) -> dict | None:
        try:
            raw = await fetch_latest(relay, {"ids": [event_id], "limit": 1})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"compromised: proof fetch from {relay} failed: {e!r}")
            return None
        if raw is None or raw.get("id") != event_id:
            return None
        try:
            return verified_proof(raw)
        except HTTPException:
            logger.warning(f"compromised: {relay} sent an invalid copy of {event_id}")
            return None

    tasks = [asyncio.create_task(_from(relay)) for relay in relays]
    try:
        for next_done in asyncio.as_completed(tasks):
            proof = await next_done
            if proof is not None:
                return proof
    finally:
        for task in tasks:
            task.cancel()
    raise HTTPException(
        status_code=422,
        detail=(
            f"No relay we asked holds event {event_id}; send the whole signed "
            "event as proof_event."
        ),
    )


async def add_compromised(
    pubkey: str, body: CompromisedPubkeyBody, added_by: str | None
) -> CompromisedPubkey:
    """Record the account, committed before returning: every later run holds it
    at 0 from then on. Fields the body leaves out keep their stored values; a
    new account needs a proof."""
    sent = body.model_fields_set
    proof: dict | None = None
    if body.proof_event is not None:
        proof = verified_proof(body.proof_event)
    elif body.proof_event_id is not None:
        proof = await fetch_proof_event(*proof_pointer(body.proof_event_id))

    async with db_session() as db:
        stored = await select_compromised_on_db(db, pubkey)
        if proof is None:
            if stored is None:
                raise HTTPException(
                    status_code=422,
                    detail="A new account needs proof_event or proof_event_id.",
                )
            proof = stored.proof_event

        def keep(name: str):
            if name in sent:
                return getattr(body, name)
            return getattr(stored, name) if stored is not None else None

        row = await upsert_compromised_on_db(
            db,
            pubkey,
            proof,
            keep("successor_pubkey"),
            keep("reason"),
            added_by,
            successor_event_id=keep("successor_event_id"),
            compromised_since=keep("compromised_since"),
        )
        await db.commit()
        forget_compromised_authors()
        await db.refresh(row)
        return row


async def update_compromised_metadata(
    pubkey: str, body: CompromisedMetadataBody
) -> CompromisedPubkey:
    """Change the successor and/or note of a marked account; its proof stays and
    nothing is zeroed. 404 when it isn't marked, 422 for a successor that is the
    account itself."""
    values = {name: getattr(body, name) for name in body.model_fields_set}
    if values.get("successor_pubkey") == pubkey:
        raise HTTPException(
            status_code=422, detail="The new account can't be the compromised one."
        )
    async with db_session() as db:
        if not values:
            row = await select_compromised_on_db(db, pubkey)
        else:
            row = await update_compromised_metadata_on_db(db, pubkey, values)
            await db.commit()
        if row is None:
            raise HTTPException(
                status_code=404, detail="That account is not marked compromised."
            )
        await db.refresh(row)
        return row


async def remove_compromised(pubkey: str) -> bool:
    """Stop holding the account at 0. Each Observer's rank-0 TA stays until that
    Observer re-asserts the account: its next run where the score changed, or
    its next full sync (admin `resync` to hurry one). Neo4j and the whitelist
    get the score back on the Observer's next run that writes it. The key's own
    follows, mutes, reports and designations, cut when it was marked, come back
    only as it publishes them again."""
    async with db_session() as db:
        removed = await delete_compromised_on_db(db, pubkey)
        await db.commit()
        forget_compromised_authors()
        return removed
