"""Observer published-state drift — diff what the relay actually holds against
each observer's `last_published`, surfacing:

  * orphans = on the relay, not in `last_published`  (relay has extra → reap set)
  * missing = in `last_published`, not on the relay  (under-delivered / baseline
              over-claim → re-asserted by a full relay sync)

Enumeration is via `strfry scan` (run in the relay pod), NOT a REQ subscription:
strfry hard-caps a filter's `limit` (`maxFilterLimit`, default 500) and TAs are
burst-published (thousands share a `created_at` second), so `until`-paginated
REQ recovers only ~5% of a large signer's set. `scan` streams the whole DB with
no limit. See the orphan handoff for the measured recall failure.

This module is stdlib-only (parse + diff) so it stays importable without any
relay/nostr deps. The operator produces the scan dump out-of-band, e.g.:

    strfry scan '{"kinds":[30382]}'            # all signers, one pass
    strfry scan '{"kinds":[30382],"authors":["<signer>"]}'   # one signer

and feeds it to `scripts/probe_relay_orphans.py`. The reap half (kind-5 over the
orphan set) will reuse `parse_ta_scan` + `diff_published`.
"""
import json
from collections import defaultdict
from typing import Iterable

from app.core.loggr import loggr

logger = loggr.get_logger(__name__)

TA_KIND = 30382


def parse_ta_scan(lines: Iterable[str]) -> dict[str, set[str]]:
    """Parse `strfry scan` JSONL (one event per line) into
    `signer_pubkey -> {observee, ...}`.

    Each kind-30382 event's author (`pubkey`) is the TA signer; its `d` tag is
    the observee. kind-30382 is parameterized-replaceable, so the relay holds ~one
    event per (signer, observee) and the set size ≈ the true published TA count.
    Malformed lines and non-30382 events are skipped.
    """
    by_signer: dict[str, set[str]] = defaultdict(set)
    n = bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if e.get("kind") != TA_KIND:
            continue
        signer = e.get("pubkey")
        if not signer:
            continue
        for tag in e.get("tags", []):
            if tag and len(tag) >= 2 and tag[0] == "d":
                by_signer[signer].add(tag[1])
                break
        n += 1
    logger.info(
        "parsed TA scan: %d events, %d signers, %d bad lines",
        n,
        len(by_signer),
        bad,
    )
    return dict(by_signer)


def orphans_of(present: set[str], last_published: list[str]) -> set[str]:
    """The orphan set for one observer: sink entries NOT in `last_published`.

    Includes GHOSTS — vanished-from-graph entries no resync can reap (resync only
    deletes `fell_off ∪ below`, both derived from truth) — plus legacy sub-cutoff
    and best-effort-write leftovers. Safe to delete wholesale: `plan_publish`
    persists `last_published` as the COMPLETE above-cutoff set each run, so a
    legitimately published observee is never absent from it.

    The inverse ("missing" = `last_published − present`, under-delivery) is
    deliberately NOT computed — that's repaired by a resync, not by these scripts.
    """
    return set(present) - set(last_published)
