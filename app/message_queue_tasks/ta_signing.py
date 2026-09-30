"""Pure Trusted-Assertion and deletion signing (nostr-sdk only, no settings/db).

Events are signed locally from the Observer's `Keys` and returned as signed
JSON, ready to send as-is: no relay client, and no nostr-sdk `Event` objects.
"""

import hashlib
import json
import time
from typing import NamedTuple

from nostr_sdk import EventBuilder, Keys, Kind, Tag  # type: ignore

from app.utils.client_tag import CLIENT_TAG

# Trusted Assertions are kind-30382 parameterized-replaceable events, keyed by
# the Observee in the `d` tag.
TA_KIND = 30382
# Coordinates per kind-5 deletion event. strfry's maxEventSize is generous, but
# bounding the tag count keeps each deletion event well under any relay limit.
DELETION_COORDS_PER_EVENT = 200
# The algorithm's "no path from the Observer" hops value. Keyed on here rather
# than on the current hop limit (8) so raising that limit needs no edit.
UNREACHABLE_HOPS = 999
DELETION_KIND = 5
DELETION_CONTENT = "dropped below cutoff"


class TaInput(NamedTuple):
    """The per-event publish inputs."""

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


class SignedEvent(NamedTuple):
    id: str
    json: str


def sign_event_json(
    kind: int, tags: list[list[str]], content: str, keys: Keys, pubkey_hex: str
) -> SignedEvent:
    """Hash and serialize in Python, calling nostr-sdk once for the Schnorr
    signature: an `EventBuilder` costs a Python→Rust crossing per tag."""
    created_at = int(time.time())
    event_id = hashlib.sha256(
        _compact_json([0, pubkey_hex, created_at, kind, tags, content]).encode()
    ).hexdigest()
    signed = _compact_json(
        {
            "id": event_id,
            "pubkey": pubkey_hex,
            "created_at": created_at,
            "kind": kind,
            "tags": tags,
            "content": content,
            "sig": keys.sign_schnorr(bytes.fromhex(event_id)),
        }
    )
    return SignedEvent(event_id, signed)


def sign_ta_json(ta_input: TaInput, keys: Keys, pubkey_hex: str) -> SignedEvent:
    return sign_event_json(TA_KIND, ta_tags(ta_input), "", keys, pubkey_hex)


def atag_deletion_tags(
    observees: list[str],
    signing_pubkey: str,
    chunk_size: int = DELETION_COORDS_PER_EVENT,
) -> list[list[list[str]]]:
    """Tags for kind-5 deletions that remove each Observee's TA by `a`-tag
    coordinate `30382:<signing_pubkey>:<observee>` — no relay fetch for event ids.

    `signing_pubkey` MUST be the pubkey the deletion is signed with: strfry only
    honours an `a`-tag delete when the coordinate's pubkey equals the deletion
    event's author. One tag list per `chunk_size` coordinates."""
    return [
        [
            *(
                ["a", f"{TA_KIND}:{signing_pubkey}:{observee}"]
                for observee in observees[i : i + chunk_size]
            ),
            [*CLIENT_TAG],
        ]
        for i in range(0, len(observees), chunk_size)
    ]


def sign_deletion_json(
    tags: list[list[str]], keys: Keys, pubkey_hex: str
) -> SignedEvent:
    return sign_event_json(DELETION_KIND, tags, DELETION_CONTENT, keys, pubkey_hex)


def build_atag_deletion_builders(
    observees: list[str],
    signing_pubkey: str,
    chunk_size: int = DELETION_COORDS_PER_EVENT,
) -> list[EventBuilder]:
    """`atag_deletion_tags` as nostr-sdk builders, for callers that sign through a
    relay client."""
    return [
        EventBuilder(kind=Kind(DELETION_KIND), content=DELETION_CONTENT).tags(
            [Tag.parse(t) for t in tags]
        )
        for tags in atag_deletion_tags(observees, signing_pubkey, chunk_size)
    ]
