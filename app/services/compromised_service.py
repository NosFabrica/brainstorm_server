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

Not covered: what the account rated still counts in GrapeRank (its follows,
mutes, reports and designations carry the influence the worker computes for
it), until the worker learns about compromised accounts.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from nostr_sdk import EventId, Keys, Nip19Event, PublicKey  # type: ignore
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.db_models import CompromisedPubkey
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
    select_observers_publishing_on_db,
)
from app.repos.compromised_pubkey_repo import (
    delete_compromised_on_db,
    list_compromised_on_db,
    select_compromised_in_on_db,
    select_compromised_on_db,
    upsert_compromised_on_db,
)
from app.repos.observer_whitelist_repo import remove_pubkey_from_whitelists_on_db
from app.repos.user_repo import clear_observer_scores, get_score_keys_on_node
from app.schemas.request_body_schemas import CompromisedPubkeyBody
from app.schemas.schemas import CompromisedStatus
from app.services.designation_service import fetch_latest
from app.utils.signed_event import SignedEventRejected, verify_signed_event

logger = loggr.get_logger(__name__)

# Signers per relay REQ. Each signer holds at most one 30382 per `d`, so a REQ
# returns ≤ this many events — under strfry's stock maxFilterLimit (500).
AUTHORS_PER_REQ = 200
_RELAY_TIMEOUT_SECONDS = 60.0


def parse_pubkey(value: str) -> str:
    """npub or hex → hex. ValueError if neither."""
    try:
        return PublicKey.parse(value.strip()).to_hex()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"{value!r} is not a pubkey") from e


def parse_event_id(value: str) -> str:
    """hex, note1… or nevent1… → hex. ValueError if none of those."""
    try:
        return EventId.parse(value.strip()).to_hex()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"{value!r} is not an event id") from e


@dataclass(frozen=True)
class Observer:
    pubkey: str  # the Observer (brainstorm_nsec.pubkey)
    signer: str  # the key its TAs are signed with
    nsec: str = field(repr=False)  # plaintext


@dataclass
class ZeroReport:
    pubkey: str
    # (observer, signer, where its TA was seen: "db" | "relay" | "both")
    observers: list[tuple[str, str, str]] = field(default_factory=list)
    relay_read: bool = True  # False: the relay read failed, DB-only selection
    published: int = 0
    acked: int = 0
    complete: bool = True  # every rank-0 TA acked (vacuously true on a dry run)
    neo4j_cleared: int = 0
    whitelists_updated: int = 0


def select_observers(
    observers: Iterable[Observer],
    published_by: set[str],
    relay_signers: set[str],
) -> list[tuple[Observer, str]]:
    """Observers holding a TA about the account, with where we saw it."""
    out = []
    for o in observers:
        in_db = o.pubkey in published_by
        on_relay = o.signer in relay_signers
        if in_db or on_relay:
            source = "both" if in_db and on_relay else "db" if in_db else "relay"
            out.append((o, source))
    return out


async def sign_zero_tas(
    pubkey: str, observers: list[Observer]
) -> AsyncIterator[SignedEvent]:
    for o in observers:
        yield sign_ta_json(
            zero_ta_input(pubkey), Keys.parse(secret_key=o.nsec), o.signer
        )
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
                        "limit": len(batch),
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
                    if n_events > len(batch):
                        logger.warning(
                            f"compromised: relay sent {n_events} for {len(batch)} signers"
                        )
                        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"compromised: relay read failed: {e!r}")
        return None
    return found


async def load_observers(db: AsyncDBSession, observer: str | None) -> list[Observer]:
    """Every Observer (or one) with its signer."""
    out = []
    for pubkey, nsec in await select_observer_nsecs_on_db(db, observer):
        try:
            signer = Keys.parse(secret_key=nsec).public_key().to_hex()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"compromised: bad nsec for observer {pubkey}: {e}")
            continue
        out.append(Observer(pubkey, signer, nsec))
    return out


async def clear_neo4j_scores(pubkey: str) -> int:
    """Remove every observer's stored score for `pubkey`; how many properties."""
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
    clear its Neo4j scores and whitelist entries. `observer` limits it to one
    Observer (the TA half only). A failed relay read falls back to the DB's
    selection (`relay_read=False`): it may miss TAs the DB lost track of."""
    relay = settings.nostr_upload_ta_events_relay
    report = ZeroReport(pubkey)
    async with db_session() as db:
        observers = await load_observers(db, observer)
        published_by = await select_observers_publishing_on_db(db, pubkey, observer)
    relay_signers: set[str] = set()
    if not db_only:
        got = await fetch_ta_signers(
            relay, pubkey, sorted({o.signer for o in observers})
        )
        report.relay_read = got is not None
        relay_signers = got or set()
    selected = select_observers(observers, published_by, relay_signers)
    report.observers = [(o.pubkey, o.signer, src) for o, src in selected]
    logger.info(
        f"compromised {pubkey}: {len(selected)} of {len(observers)} observers hold a "
        f"TA ({len(published_by)} by DB, {len(relay_signers)} on relay, "
        f"relay_read={report.relay_read}) apply={apply}"
    )
    if not apply:
        return report

    if selected:
        stats = await publish_events(
            relay, sign_zero_tas(pubkey, [o for o, _ in selected])
        )
        report.published, report.acked = stats.n_events, stats.n_acked
        report.complete = not stats.aborted and stats.n_unacked == 0
        if not report.complete:
            logger.error(
                f"compromised {pubkey}: {stats.n_acked}/{stats.n_events} rank-0 TAs "
                f"acked: {stats.failure_samples}"
            )
    if observer is None:
        report.neo4j_cleared = await clear_neo4j_scores(pubkey)
        async with db_session() as db:
            report.whitelists_updated = await remove_pubkey_from_whitelists_on_db(
                db, pubkey
            )
            await db.commit()
    logger.info(
        f"compromised {pubkey}: published {report.published}, acked {report.acked}, "
        f"neo4j props cleared {report.neo4j_cleared}, whitelists "
        f"{report.whitelists_updated}"
    )
    return report


async def zero_compromised_in_background(pubkey: str) -> None:
    """For the admin route: run after the response, never raise."""
    try:
        await zero_compromised(pubkey)
    except Exception as e:  # noqa: BLE001
        logger.error(f"compromised {pubkey}: zeroing failed: {e!r}")


# --- admin ---------------------------------------------------------------------


async def check_compromised(pubkeys: list[str]) -> list[CompromisedStatus]:
    """One status per pubkey (hex), in the order asked: the public read the UI
    uses to flag a compromised account."""
    async with db_session() as db:
        marked = {r.pubkey: r for r in await select_compromised_in_on_db(db, pubkeys)}
    out = []
    for pk in pubkeys:
        row = marked.get(pk)
        if row is None:
            out.append(CompromisedStatus(pubkey=pk, compromised=False))
        else:
            out.append(
                CompromisedStatus(
                    pubkey=pk,
                    compromised=True,
                    since=row.created_at,
                    proof_event_id=row.proof_event_id,
                    proof_event=row.proof_event,
                    successor_pubkey=row.successor_pubkey,
                )
            )
    return out


async def list_compromised() -> list[CompromisedPubkey]:
    async with db_session() as db:
        return await list_compromised_on_db(db)


def proof_pointer(value: str) -> tuple[str, list[str]]:
    """An event id (hex, note1… or nevent1…) → (hex id, the nevent's relay hints)."""
    value = value.strip()
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
    from, our own, then `compromised_proof_relays`. 422 when none holds a valid
    copy."""
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
    for relay in relays:
        try:
            raw = await fetch_latest(relay, {"ids": [event_id], "limit": 1})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"compromised: proof fetch from {relay} failed: {e!r}")
            continue
        if raw is None or raw.get("id") != event_id:
            continue
        try:
            return verified_proof(raw)
        except HTTPException:
            logger.warning(f"compromised: {relay} sent an invalid copy of {event_id}")
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
        )
        await db.commit()
        await db.refresh(row)
        return row


async def remove_compromised(pubkey: str) -> bool:
    """Stop holding the account at 0. Each Observer's rank-0 TA stays until that
    Observer re-asserts the account: its next run where the score changed, or
    its next full sync (admin `resync` to hurry one). Neo4j and the whitelist
    get the score back on the Observer's next run that writes it."""
    async with db_session() as db:
        removed = await delete_compromised_on_db(db, pubkey)
        await db.commit()
        return removed
