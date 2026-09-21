"""Pure membership + wire-shape logic for Trusted Lists.

No I/O. Ports tapestry's `applyDisputesFunction`
(src/api/trustedList/refreshPinnedTags.js:99) and the kind-30392 composition in
`buildAndPublishTL` (src/api/trustedList/index.js:116), with the divergences
recorded in ADR `trusted-lists/0001` D5.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from app.services.tagging_parse import is_applied, is_disputed

TRUSTED_LIST_KIND = 30392

# Not tapestry's "pinned-tag-membership". These lists aren't pin-derived, and
# tapestry's retraction sweep keys on the `tl-pin-` prefix — sharing either
# literal would make the two derivations collide on a relay mirroring both.
TRUSTED_LIST_METRIC = "tag-membership"
D_TAG_PREFIX = "tl-tag"

RETRACTED_MARKER = ("status", "retracted")

# Fallback rigor, used when an Observer has no resolvable GrapeRank preset.
# Matches the DEFAULT preset's seeded value, and tapestry's hardcoded constant,
# so an unconfigured Observer scores identically in both estates.
DEFAULT_RIGOR = 0.5


def _format_rigor(rigor: float) -> str:
    """Rigor on the wire, without float noise.

    `str(0.65)` is fine, but a rigor arriving as e.g. 0.30000000000000004 from
    an operator edit would publish that verbatim and no consumer could
    reproduce the score from it. Trim to a sane precision and drop the tail.
    """
    return f"{rigor:.6f}".rstrip("0").rstrip(".")


def _round_half_up(value: float) -> int:
    """Round half away from zero, matching JavaScript's `Math.round`.

    Python's built-in `round` is banker's rounding — `round(0.5) == 0` — so
    using it here would disagree with tapestry on every exact .5 boundary.
    Scores are clamped non-negative before this is called, so `floor(x + 0.5)`
    is the whole of `Math.round`'s behaviour for our domain.
    """
    return math.floor(value + 0.5)


@dataclass(frozen=True)
class Member:
    pubkey: str
    applications: int
    disputes: int
    # Weighted confidence on the estate's Rank quantum: a 0-100 integer.
    score: int


def compute_d_tag(observer: str, tag_author_pubkey: str, slug: str) -> str:
    """`tl-tag-<observer8>-<tagAuthor8>-<slug>`.

    Encodes the (Observer, tag-by-author+slug) identity: two Observers over the
    same tag get distinct slots, and same-slug tags by different authors stay
    distinct. The 8-char truncation is inherited from tapestry's deployed shape
    and can collide in principle — ADR D5 accepts that rather than forking the
    wire format.
    """
    return f"{D_TAG_PREFIX}-{observer[:8]}-{tag_author_pubkey[:8]}-{slug}"


@dataclass
class _Tally:
    applications: int = 0
    disputes: int = 0
    # Sigma w over the pair's live taggings.
    weighted_input: float = 0.0
    # Sigma (w * r), r = +1 applied / -1 disputed.
    weighted_sum: float = 0.0


def compute_score(
    weighted_sum: float, weighted_input: float, rigor: float = DEFAULT_RIGOR
) -> int:
    """`round(max(average * certainty, 0) * 100)` as a 0-100 integer.

    Split out from the fold so the parity vectors can exercise the arithmetic
    directly. Zero input scores 0 rather than dividing by it.

    `rigor` is the Observer's GrapeRank rigor. Lower rigor reaches confidence
    on less trust mass (PERMISSIVE, 0.3), higher rigor demands more
    (RESTRICTIVE, 0.65). Note the degenerate end: `rigor = 1.0` makes
    `certainty` identically 0, so every member scores 0 and every list empties.
    The schema permits it; the resolver warns about it.
    """
    if weighted_input == 0:
        return 0
    average = weighted_sum / weighted_input
    certainty = 1 - rigor**weighted_input
    return _round_half_up(max(average * certainty, 0.0) * 100)


def compute_members(
    taggings: list[tuple[str, float, float]],
    cutoff: int,
    rigor: float = DEFAULT_RIGOR,
) -> list[Member]:
    """Bucket assertions per target, score them, apply the membership predicate.

    `taggings` are `(target_pubkey, polarity, weight)` triples, where weight is
    the asserter's trust weight in this Observer's web of trust — Influence on
    the Rank quantum, so in [0, 1].

    Scoring is GrapeRank's interpreter formula applied single-hop over one
    (tag, target) pair (ADR D12):

        input     = Sigma w
        average   = Sigma (w * r) / input          (r = +1 applied, -1 disputed)
        certainty = 1 - rigor ** input
        score     = round(max(average * certainty, 0) * 100)   -> 0..100

    Membership is three clauses, not two: `applications >= cutoff` AND
    `applications > disputes` AND `score >= 1`. The middle clause is inherited
    — tapestry's `certainty` method chains off `applyDisputesFunction`
    (refreshPinnedTags.js:112), so the v1 count predicate still gates before
    the score does. ADR D12 as drafted states only the outer two; the shipped
    tapestry code has all three, and matching the code is what keeps the two
    implementations agreeing on a target with more disputes than applications
    but a high-weight applier.

    Neutral-polarity assertions (the reserved open interval) count as neither
    and contribute no weight. Order is score desc, then pubkey asc — stable
    across runs so an unchanged membership republishes byte-identically.
    """
    tally: dict[str, _Tally] = {}
    for target_pubkey, polarity, weight in taggings:
        entry = tally.setdefault(target_pubkey, _Tally())
        if is_applied(polarity):
            entry.applications += 1
            entry.weighted_input += weight
            entry.weighted_sum += weight
        elif is_disputed(polarity):
            entry.disputes += 1
            entry.weighted_input += weight
            entry.weighted_sum -= weight

    members = []
    for pubkey, t in tally.items():
        if not (t.applications >= cutoff and t.applications > t.disputes):
            continue
        score = compute_score(t.weighted_sum, t.weighted_input, rigor)
        if score < 1:
            # Net-negative, zero-mass and exact-split pairs all round to 0 and
            # drop off the list entirely — the weighted successor of v1's
            # `applications > disputes`.
            continue
        members.append(
            Member(
                pubkey=pubkey,
                applications=t.applications,
                disputes=t.disputes,
                score=score,
            )
        )
    members.sort(key=lambda m: (-m.score, m.pubkey))
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
    rigor: float = DEFAULT_RIGOR,
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
        # No score or rigor on a retraction: there is no membership to score.
        tags.append(list(RETRACTED_MARKER))
        return tags
    tags.append(["rigor", _format_rigor(rigor)])
    # `["p", <pubkey>, "", "<score>"]` — the relay slot stays empty and the
    # score rides third, as a string. This is the layout tapestry's reader
    # already parses (trustedList/index.js:135-140).
    tags.extend([["p", m.pubkey, "", str(m.score)] for m in members])
    return tags


def build_trusted_list_content(members: list[Member]) -> str:
    """The `content` payload: per-member endorsement/dispute counts plus score.

    Consumers that only read `p` tags ignore this; it exists so a reader can see
    *how contested* each membership is without re-deriving it from raw taggings.
    Treat it as advisory — tapestry is emptying its own TL content as
    duplicative, and the `p` tags are the canonical member list.
    """
    return json.dumps(
        {
            "members": [
                {
                    "pubkey": m.pubkey,
                    "endorsements": m.applications,
                    "disputes": m.disputes,
                    "score": m.score,
                }
                for m in members
            ]
        }
    )
