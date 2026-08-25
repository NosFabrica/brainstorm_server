"""Pure membership + wire-shape logic for Trusted Lists.

No I/O. Ports tapestry's `applyDisputesFunction`
(src/api/trustedList/refreshPinnedTags.js:99) and the kind-30392 composition in
`buildAndPublishTL` (src/api/trustedList/index.js:116), with the divergences
recorded in ADR `trusted-lists/0001` D5.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.services.tagging_parse import is_applied, is_disputed

TRUSTED_LIST_KIND = 30392

# Not tapestry's "pinned-tag-membership". These lists aren't pin-derived, and
# tapestry's retraction sweep keys on the `tl-pin-` prefix — sharing either
# literal would make the two derivations collide on a relay mirroring both.
TRUSTED_LIST_METRIC = "tag-membership"
D_TAG_PREFIX = "tl-tag"

RETRACTED_MARKER = ("status", "retracted")


@dataclass(frozen=True)
class Member:
    pubkey: str
    applications: int
    disputes: int


def compute_d_tag(observer: str, tag_author_pubkey: str, slug: str) -> str:
    """`tl-tag-<observer8>-<tagAuthor8>-<slug>`.

    Encodes the (Observer, tag-by-author+slug) identity: two Observers over the
    same tag get distinct slots, and same-slug tags by different authors stay
    distinct. The 8-char truncation is inherited from tapestry's deployed shape
    and can collide in principle — ADR D5 accepts that rather than forking the
    wire format.
    """
    return f"{D_TAG_PREFIX}-{observer[:8]}-{tag_author_pubkey[:8]}-{slug}"


def compute_members(taggings: list[tuple[str, float]], cutoff: int) -> list[Member]:
    """Bucket assertions per target and apply the membership predicate.

    A target is a member iff `applications >= cutoff AND applications > disputes`.
    Neutral-polarity assertions (the reserved open interval) count as neither.
    Order is applications desc, then pubkey asc — stable across runs so an
    unchanged membership republishes byte-identically.
    """
    tally: dict[str, list[int]] = {}
    for target_pubkey, polarity in taggings:
        entry = tally.setdefault(target_pubkey, [0, 0])
        if is_applied(polarity):
            entry[0] += 1
        elif is_disputed(polarity):
            entry[1] += 1

    members = [
        Member(pubkey=pubkey, applications=counts[0], disputes=counts[1])
        for pubkey, counts in tally.items()
        if counts[0] >= cutoff and counts[0] > counts[1]
    ]
    members.sort(key=lambda m: (-m.applications, m.pubkey))
    return members


def build_trusted_list_tags(
    *,
    observer: str,
    tag_event_id: str,
    tag_author_pubkey: str,
    slug: str,
    name: str,
    description: str,
    members: list[Member],
    cutoff: int,
    min_rank: int,
    retracted: bool = False,
) -> list[list[str]]:
    """The kind-30392 tag list, per ADR D5.

    A retraction is the same shape with zero `p` tags plus the retracted marker
    — an empty-membership replacement at the same coordinate, since kind-30392
    is parameterized-replaceable and there is no "delete" for a claim.
    """
    tags: list[list[str]] = [
        ["d", compute_d_tag(observer, tag_author_pubkey, slug)],
        ["title", name],
        ["description", description],
        ["metric", TRUSTED_LIST_METRIC],
        ["observer", observer],
        ["source-tag", tag_event_id, tag_author_pubkey, slug],
        ["cutoff", str(cutoff)],
        ["min-rank", str(min_rank)],
    ]
    if retracted:
        tags.append(list(RETRACTED_MARKER))
        return tags
    tags.extend([["p", m.pubkey] for m in members])
    return tags


def build_trusted_list_content(members: list[Member]) -> str:
    """The `content` payload: per-member endorsement/dispute counts.

    Consumers that only read `p` tags ignore this; it exists so a reader can see
    *how contested* each membership is without re-deriving it from raw taggings.
    """
    return json.dumps(
        {
            "members": [
                {
                    "pubkey": m.pubkey,
                    "endorsements": m.applications,
                    "disputes": m.disputes,
                }
                for m in members
            ]
        }
    )
