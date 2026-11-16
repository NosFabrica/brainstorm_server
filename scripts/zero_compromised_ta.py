"""Re-sign every Observer's Trusted Assertion about a compromised account at rank 0.

No GrapeRank run: for each Observer in this environment's DB that holds a
kind-30382 about a compromised account, sign a replacement with that Observer's
key — `rank 0`, every count 0, no `hops` (the same shape the pipeline's
zero-score events use) — and publish it to the TA relay. A replaceable event
displaces the old score wherever it propagates; the search relay mirrors it.

The compromised accounts are hardcoded in `COMPROMISED`, each with the event
that proves the compromise. An entry without a valid proof id stops the script.

An Observer is selected if EITHER
  - its `last_published_pubkeys` holds the account (what the pipeline thinks it
    published), or
  - the TA relay holds a 30382 `d=<account>` signed by its key (catches orphans
    the DB lost track of). The relay is shared across environments, so it is
    asked only about this environment's signers, ≤200 authors per REQ.

Run inside a brainstorm-server pod of each environment (staging, production);
needs `.env` + DB + the nsec encryption key file. Dry-run by default:

    poetry run python -m scripts.zero_compromised_ta
    poetry run python -m scripts.zero_compromised_ta --apply
    poetry run python -m scripts.zero_compromised_ta --apply --observer <hex>

Limits — this is the stopgap, not the pipeline change:
  - The zero holds only until that Observer's next publish that re-asserts the
    account: a full sync (`FULL_SYNC_EVERY_N_RUNS`, admin `resync`), or a
    scheduled run in which its score changed. Re-run the script after such runs,
    or ship the pipeline change that keeps compromised accounts at 0.
  - A run publishing at the moment this script runs can overwrite the zero with
    the old score. Re-running is safe (idempotent); do it once in-flight runs end.
  - Neo4j scores (`influence_<observer>`) and what the account vouched for are
    untouched; both change only when GrapeRank runs again.

Exit codes: 0 = done (or dry run), 1 = a relay read or publish did not complete,
2 = bad `COMPROMISED` entry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path

# Allow running as a plain script: `python scripts/zero_compromised_ta.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nostr_sdk import EventId, Keys, PublicKey  # type: ignore  # noqa: E402

from app.message_queue_tasks.ta_signing import (  # noqa: E402
    TA_KIND,
    UNREACHABLE_HOPS,
    SignedEvent,
    TaInput,
    sign_ta_json,
)

# npub (or hex) of each compromised account -> id of the event proving the
# compromise (hex, note1… or nevent1…). Add an entry, ship, run the script.
COMPROMISED: dict[str, str] = {
    "npub18ams6ewn5aj2n3wt2qawzglx9mr4nzksxhvrdc4gzrecw7n5tvjqctp424": "",
}

# Signers per relay REQ. Each signer holds at most one 30382 per `d`, so a REQ
# returns ≤ this many events — under strfry's stock maxFilterLimit (500).
AUTHORS_PER_REQ = 200
_RELAY_TIMEOUT_SECONDS = 15.0
_PUBKEY_BYTES = 32


@dataclass(frozen=True)
class Compromised:
    pubkey: str  # hex — the TA's `d`
    proof: str  # hex event id


@dataclass(frozen=True)
class Observer:
    pubkey: str  # the Observer (brainstorm_nsec.pubkey)
    signer: str  # the key its TAs are signed with
    nsec: str  # plaintext


class BadEntry(ValueError):
    pass


def compromised_targets(entries: dict[str, str]) -> list[Compromised]:
    """Validate `COMPROMISED`: every key a pubkey, every proof an event id."""
    out = []
    for key, proof in entries.items():
        try:
            pubkey = PublicKey.parse(key).to_hex()
        except Exception as e:  # noqa: BLE001
            raise BadEntry(f"{key!r} is not a pubkey: {e}") from e
        if not proof or not proof.strip():
            raise BadEntry(f"{key} has no proof event id")
        try:
            proof_hex = EventId.parse(proof.strip()).to_hex()
        except Exception as e:  # noqa: BLE001
            raise BadEntry(f"{key}: proof {proof!r} is not an event id: {e}") from e
        out.append(Compromised(pubkey, proof_hex))
    return out


def holds_pubkey(blob: bytes | None, pubkey: str) -> bool:
    """Whether a packed `last_published_pubkeys` blob lists `pubkey` — at a
    32-byte boundary, so a match straddling two entries doesn't count."""
    if not blob:
        return False
    needle = bytes.fromhex(pubkey)
    i = blob.find(needle)
    while i != -1:
        if i % _PUBKEY_BYTES == 0:
            return True
        i = blob.find(needle, i + 1)
    return False


def zero_ta(pubkey: str) -> TaInput:
    """The rank-0 TA: every count 0 and no `hops`, as the pipeline's zero-score
    events (`get_zero_score_events_for_pubkeys`)."""
    return TaInput(pubkey, 0, 0, 0, 0, UNREACHABLE_HOPS)


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
    targets: list[tuple[Observer, Compromised]],
) -> AsyncIterator[SignedEvent]:
    for observer, target in targets:
        keys = Keys.parse(secret_key=observer.nsec)
        yield sign_ta_json(zero_ta(target.pubkey), keys, observer.signer)
        await asyncio.sleep(0)


# --- I/O -----------------------------------------------------------------------


async def fetch_ta_signers(
    url: str, pubkey: str, signers: list[str]
) -> set[str] | None:
    """Which of `signers` the relay holds a 30382 `d=<pubkey>` from. `None` if
    any REQ failed, timed out or may have been truncated."""
    import websockets

    found: set[str] = set()
    try:
        async with asyncio.timeout(_RELAY_TIMEOUT_SECONDS * 4):
            async with websockets.connect(url, max_size=None) as ws:
                for n, start in enumerate(range(0, len(signers), AUTHORS_PER_REQ)):
                    batch = signers[start : start + AUTHORS_PER_REQ]
                    sub = f"zero-compromised-{n}"
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
                            print(f"# relay CLOSED {sub}: {msg[2:]}", file=sys.stderr)
                            return None
                    if n_events > len(batch):
                        print(
                            f"# relay sent {n_events} for {len(batch)} signers",
                            file=sys.stderr,
                        )
                        return None
    except Exception as e:  # noqa: BLE001
        print(f"# relay read failed: {e!r}", file=sys.stderr)
        return None
    return found


async def load_observers(db, observer: str | None) -> list[Observer]:
    """Every Observer with its signer — the multi-MB blob is not read."""
    from sqlalchemy import select

    from app.db_models import BrainstormNsec
    from app.utils.encryption import decrypt_nsec

    stmt = select(
        BrainstormNsec.pubkey, BrainstormNsec.nsec, BrainstormNsec.encrypted_nsec
    )
    if observer:
        stmt = stmt.where(BrainstormNsec.pubkey == observer)
    out = []
    for pubkey, nsec, encrypted in (await db.execute(stmt)).all():
        try:
            plain = decrypt_nsec(encrypted) if encrypted else nsec
            signer = Keys.parse(secret_key=plain).public_key().to_hex()
        except Exception as e:  # noqa: BLE001
            print(f"# bad nsec for observer {pubkey}: {e}", file=sys.stderr)
            continue
        out.append(Observer(pubkey, signer, plain))
    return out


async def observers_publishing(db, pubkey: str, observer: str | None) -> set[str]:
    """Observers whose `last_published_pubkeys` lists `pubkey`. Postgres finds
    the candidate rows; only those blobs are read, to check the boundary."""
    from sqlalchemy import select, text

    from app.db_models import BrainstormNsec

    stmt = select(BrainstormNsec.pubkey, BrainstormNsec.last_published_pubkeys).where(
        text("position(:needle IN last_published_pubkeys) > 0").bindparams(
            needle=bytes.fromhex(pubkey)
        )
    )
    if observer:
        stmt = stmt.where(BrainstormNsec.pubkey == observer)
    rows = (await db.execute(stmt)).all()
    return {obs for obs, blob in rows if holds_pubkey(blob, pubkey)}


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="Sign and publish.")
    parser.add_argument("--observer", help="Only this Observer (hex).")
    parser.add_argument(
        "--db-only",
        action="store_true",
        help="Skip the relay read (if it fails); misses TAs the DB lost track of.",
    )
    args = parser.parse_args()

    try:
        targets = compromised_targets(COMPROMISED)
    except BadEntry as e:
        print(f"COMPROMISED: {e}", file=sys.stderr)
        return 2

    from app.core.config import settings
    from app.core.database import db_session
    from app.message_queue_tasks.relay_publisher import publish_events
    from app.utils.encryption import load_keys_from_file

    relay = settings.nostr_upload_ta_events_relay
    load_keys_from_file()  # standalone process: bootstrap the nsec key file
    plan: list[tuple[Observer, Compromised]] = []
    async with db_session() as db:
        observers = await load_observers(db, args.observer)
        print(f"# {len(observers)} observers, TA relay {relay}", file=sys.stderr)
        signers = sorted({o.signer for o in observers})
        for target in targets:
            published_by = await observers_publishing(db, target.pubkey, args.observer)
            relay_signers: set[str] = set()
            if not args.db_only:
                got = await fetch_ta_signers(relay, target.pubkey, signers)
                if got is None:
                    print(
                        "relay read incomplete; re-run, or pass --db-only",
                        file=sys.stderr,
                    )
                    return 1
                relay_signers = got
            selected = select_observers(observers, published_by, relay_signers)
            print(
                f"# {target.pubkey} (proof {target.proof}): {len(selected)} observers "
                f"({len(published_by)} by DB, {len(relay_signers)} on relay)",
                file=sys.stderr,
            )
            for o, source in selected:
                print(f"{target.pubkey}\t{o.pubkey}\t{o.signer}\t{source}")
                plan.append((o, target))

    if not args.apply:
        print(
            f"# dry run: {len(plan)} TAs would be zeroed; --apply to publish",
            file=sys.stderr,
        )
        return 0
    if not plan:
        return 0

    stats = await publish_events(relay, sign_zero_tas(plan))
    print(
        f"# published {stats.n_events}, acked {stats.n_acked}, "
        f"rejected {dict(stats.rejected)}",
        file=sys.stderr,
    )
    if stats.aborted or stats.n_unacked:
        print(f"# INCOMPLETE: {stats.failure_samples}; re-run", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
