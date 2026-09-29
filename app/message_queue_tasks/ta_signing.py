"""Pure, process-safe Trusted-Assertion signing.

Deliberately depends on **nostr-sdk only** (no settings/db/vespa/redis): the
functions here are the worker bodies for a `ProcessPoolExecutor`, so on a
`spawn` platform the child re-imports this module and we want that import to be
cheap and side-effect-free. The orchestration that *reads settings* and decides
whether to parallelise lives in `upload_nostr_events.py`.

`sign_ta_shard` signs locally from a `Keys` parsed from the Observer's nsec, so
no relay-connected client is involved and the nsec never leaves the process
tree.
"""

import asyncio
import hashlib
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from typing import NamedTuple

from nostr_sdk import Event, EventBuilder, Keys, Kind, Tag  # type: ignore

from app.utils.client_tag import CLIENT_TAG, client_tag

# Trusted Assertions are kind-30382 parameterized-replaceable events, keyed by
# the Observee in the `d` tag.
TA_KIND = 30382
# Coordinates per kind-5 deletion event. strfry's maxEventSize is generous, but
# bounding the tag count keeps each deletion event well under any relay limit.
DELETION_COORDS_PER_EVENT = 200
# The algorithm's "no path from the Observer" hops value. Keyed on here rather
# than on the current hop limit (8) so raising that limit needs no edit.
UNREACHABLE_HOPS = 999
# Signed TAs turned back into `Event`s between event-loop yields: Event.from_json
# costs ~90µs, so ~10ms of work per chunk.
PARSE_CHUNK = 100


class TaInput(NamedTuple):
    """The per-event publish inputs — a plain picklable tuple so a shard ships
    cheaply across the process boundary."""

    observee: str  # the `d` tag
    rank: int
    followers: int
    reporters: int
    muters: int
    hops: int


def ta_tags(ta_input: TaInput) -> list[list[str]]:
    """The single source of truth for a TA's tags, shared by every signing path
    so all of them produce content-equivalent events.

    `hops` is omitted at the unreachable sentinel so a consuming client never has
    to special-case it — an absent tag means "no path", full stop."""
    tags = [
        ["d", ta_input.observee],
        ["rank", str(ta_input.rank)],
        ["followers", str(ta_input.followers)],
        ["reporters", str(ta_input.reporters)],
        ["muters", str(ta_input.muters)],
        [*CLIENT_TAG],  # a copy: callers own the returned lists
    ]
    if ta_input.hops < UNREACHABLE_HOPS:
        tags.append(["hops", str(ta_input.hops)])
    return tags


def build_ta_event_builder(ta_input: TaInput) -> EventBuilder:
    """A TA as an nostr-sdk builder, for the few paths that sign through a relay
    client. Bulk signing uses `sign_ta_json`, which is ~4x faster."""
    tags = [Tag.parse(t) for t in ta_tags(ta_input)]
    return EventBuilder(kind=Kind(TA_KIND), content="").tags(tags)


def _compact_json(value: object) -> str:
    # NIP-01 serialization: no whitespace, UTF-8 as-is, and json's escapes
    # (\n \" \\ \r \t \b \f, other control chars as \u00XX) are the ones it
    # specifies.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def sign_ta_json(ta_input: TaInput, keys: Keys, pubkey_hex: str) -> str:
    """Build and sign one TA, returning the signed event's JSON.

    Hashes and serializes in Python and calls nostr-sdk once, for the Schnorr
    signature: an `EventBuilder` costs a Python→Rust crossing per tag, which
    made it ~4x slower per TA."""
    created_at = int(time.time())
    tags = ta_tags(ta_input)
    event_id = hashlib.sha256(
        _compact_json([0, pubkey_hex, created_at, TA_KIND, tags, ""]).encode()
    ).hexdigest()
    return _compact_json(
        {
            "id": event_id,
            "pubkey": pubkey_hex,
            "created_at": created_at,
            "kind": TA_KIND,
            "tags": tags,
            "content": "",
            "sig": keys.sign_schnorr(bytes.fromhex(event_id)),
        }
    )


def build_atag_deletion_builders(
    observees: list[str],
    signing_pubkey: str,
    chunk_size: int = DELETION_COORDS_PER_EVENT,
) -> list[EventBuilder]:
    """Kind-5 deletion events that remove each Observee's TA by `a`-tag
    coordinate `30382:<signing_pubkey>:<observee>` — no relay fetch for event ids.

    `signing_pubkey` MUST be the pubkey the deletion is signed with: strfry only
    honours an `a`-tag delete when the coordinate's pubkey equals the deletion
    event's author. One builder per `chunk_size` coordinates."""
    builders: list[EventBuilder] = []
    for i in range(0, len(observees), chunk_size):
        tags = [
            Tag.parse(["a", f"{TA_KIND}:{signing_pubkey}:{observee}"])
            for observee in observees[i : i + chunk_size]
        ]
        tags.append(client_tag())
        builders.append(
            EventBuilder(kind=Kind(5), content="dropped below cutoff").tags(tags)
        )
    return builders


def sign_ta_shard(inputs: list[TaInput], nsec: str) -> list[str]:
    """Build + locally sign a shard of TAs. Returns signed-event JSON strings
    (JSON, not `Event`, so results pickle back from a worker process)."""
    keys = Keys.parse(secret_key=nsec)
    pubkey_hex = keys.public_key().to_hex()
    return [sign_ta_json(ta_input, keys, pubkey_hex) for ta_input in inputs]


def _shard(inputs: list[TaInput], n_shards: int) -> list[list[TaInput]]:
    """Split into at most `n_shards` contiguous, near-equal chunks (no empties)."""
    n_shards = max(1, min(n_shards, len(inputs)))
    size, extra = divmod(len(inputs), n_shards)
    shards: list[list[TaInput]] = []
    start = 0
    for i in range(n_shards):
        end = start + size + (1 if i < extra else 0)
        shards.append(inputs[start:end])
        start = end
    return shards


async def sign_ta_events_parallel(
    inputs: list[TaInput],
    nsec: str,
    max_workers: int | None = None,
) -> list[Event]:
    """Sign a large batch by sharding across a `ProcessPoolExecutor`.

    Each worker locally signs its shard (nsec stays inside the server's child
    processes — no secret over the network). Offloading to processes keeps the
    GIL-holding nostr-sdk signing off the event loop, so concurrent requests are
    not starved during a big sign."""
    if not inputs:
        return []
    workers = max(1, max_workers or os.cpu_count() or 1)
    shards = _shard(inputs, workers)
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor(max_workers=len(shards)) as pool:
        signed_per_shard = await asyncio.gather(
            *(
                loop.run_in_executor(pool, sign_ta_shard, shard, nsec)
                for shard in shards
            )
        )
    # Parsing is now the bulk of a large run's parent-side time (100k TAs ≈ 9s),
    # so yield between chunks rather than hold the loop for all of it.
    signed = [j for shard in signed_per_shard for j in shard]
    events: list[Event] = []
    for start in range(0, len(signed), PARSE_CHUNK):
        events.extend(Event.from_json(j) for j in signed[start : start + PARSE_CHUNK])
        await asyncio.sleep(0)
    return events
