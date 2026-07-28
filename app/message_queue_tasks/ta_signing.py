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
import os
from concurrent.futures import ProcessPoolExecutor
from typing import NamedTuple

from nostr_sdk import Event, EventBuilder, Keys, Kind, Tag  # type: ignore

# Trusted Assertions are kind-30382 parameterized-replaceable events, keyed by
# the Observee in the `d` tag.
TA_KIND = 30382
# Coordinates per kind-5 deletion event. strfry's maxEventSize is generous, but
# bounding the tag count keeps each deletion event well under any relay limit.
DELETION_COORDS_PER_EVENT = 200
# The algorithm's "no path from the Observer" hops value. Keyed on here rather
# than on the current hop limit (8) so raising that limit needs no edit.
UNREACHABLE_HOPS = 999


class TaInput(NamedTuple):
    """The per-event publish inputs — a plain picklable tuple so a shard ships
    cheaply across the process boundary."""

    observee: str  # the `d` tag
    rank: int
    followers: int
    reporters: int
    muters: int
    hops: int


def build_ta_event_builder(ta_input: TaInput) -> EventBuilder:
    """The single source of truth for a TA's kind/tags, shared by the sequential
    and parallel paths so both branches produce content-equivalent events.

    `hops` is omitted at the unreachable sentinel so a consuming client never has
    to special-case it — an absent tag means "no path", full stop."""
    tags = [
        Tag.parse(["d", ta_input.observee]),
        Tag.parse(["rank", str(ta_input.rank)]),
        Tag.parse(["followers", str(ta_input.followers)]),
        Tag.parse(["reporters", str(ta_input.reporters)]),
        Tag.parse(["muters", str(ta_input.muters)]),
    ]
    if ta_input.hops < UNREACHABLE_HOPS:
        tags.append(Tag.parse(["hops", str(ta_input.hops)]))
    return EventBuilder(kind=Kind(TA_KIND), content="").tags(tags)


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
        builders.append(
            EventBuilder(kind=Kind(5), content="dropped below cutoff").tags(tags)
        )
    return builders


def sign_ta_shard(inputs: list[TaInput], nsec: str) -> list[str]:
    """Build + locally sign a shard of TAs. Returns signed-event JSON strings
    (JSON, not `Event`, so results pickle back from a worker process)."""
    keys = Keys.parse(secret_key=nsec)
    return [
        build_ta_event_builder(ta_input).sign_with_keys(keys).as_json()
        for ta_input in inputs
    ]


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
    return [Event.from_json(j) for shard in signed_per_shard for j in shard]
