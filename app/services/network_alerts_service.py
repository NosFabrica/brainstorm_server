"""Network alerts: bad actors who gained a following inside the trust network.

v1 of the Brainstorm dashboard "Network Alerts" panel. Answers one question for
an observer (Alice): which pubkeys in my network carry more verified reports
than their reach justifies?

The panel has two sections and this service maps 1:1 onto them:

- **Direct follows** — Alice follows them herself. Most actionable: she can
  unfollow directly.
- **Extended network** — she doesn't follow them, but they clear the GrapeRank
  cutoff from her perspective, i.e. someone she trusts vouches for them.

A pubkey qualifying for both is reported only under direct follows.

The threshold needs a verified follower count. It normally comes straight off
the node — `trusted_followers_<observer>`, written by the GrapeRank result
writer — but it is absent for any observer whose first run carrying that
property hasn't landed. Those rows are counted from the graph instead, bounded
so a popular account can't dominate the response. See `_resolve_follower_counts`.

Coalescing a missing count to 0 was the obvious alternative and is wrong: it
pins N at its floor for everyone, which over-alerts on exactly the large
accounts the scaling exists to protect.
"""

from collections import defaultdict

from app.core.loggr import loggr
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.user_repo import (
    BASE_REPORTER_THRESHOLD,
    FOLLOWERS_PER_EXTRA_REPORT,
    MAX_ALERT_CANDIDATES,
    count_above_cutoff_followers_capped,
    count_verified_muters,
    get_network_alert_candidates,
)
from app.schemas.request_response_schemas import (
    NetworkAlertItem,
    NetworkAlertsData,
)
from app.services.verified_cutoffs import VerifiedCutoffs
from app.utils.neo4j_values import safe_float, safe_int

logger = loggr.get_logger(__name__)

# The floor (BASE_REPORTER_THRESHOLD, 4) and the grain
# (FOLLOWERS_PER_EXTRA_REPORT, 500) both live in user_repo, beside the candidate
# prefilter that is derived from them.

DEFAULT_ALERT_LIMIT = 100
MAX_ALERT_LIMIT = 500


def reporter_threshold_for(verified_followers: int) -> int:
    """N = 4 + floor(verified_followers / 500)."""
    return BASE_REPORTER_THRESHOLD + verified_followers // FOLLOWERS_PER_EXTRA_REPORT


def follower_count_cap_for(verified_reporters: int) -> int:
    """Point past which counting a candidate's verified followers is pointless.

    A row alerts when ``verified_reporters > 4 + floor(vfc / 500)``, which
    rearranges to ``vfc < 500 * (verified_reporters - 4)``. Once that many
    verified followers have been counted the row cannot alert, whatever the
    true total is — so the count stops there.

    Always >= 500, because candidates are prefiltered to
    ``verified_reporters >= MIN_ALERT_REPORTERS`` (5). A candidate below that
    line would make this zero or negative, which Cypher rejects as a LIMIT.
    """
    return FOLLOWERS_PER_EXTRA_REPORT * (verified_reporters - BASE_REPORTER_THRESHOLD)


async def _resolve_follower_counts(
    session,
    candidates: list[dict],
    influence_key: str,
    cutoffs: VerifiedCutoffs,
) -> dict[str, int]:
    """Verified follower count per candidate pubkey, for rows that can alert.

    Stored value wins. Anything missing it is counted from the graph, capped at
    `follower_count_cap_for(...)`. Cypher can't vary a LIMIT per row, so
    candidates are bucketed by identical cap and each bucket is one query —
    typically one or two, since the cap only varies with the reporter count.

    A pubkey is absent from the result when its count reached the cap (true
    count >= cap) or no row came back at all. Either way it cannot alert, so the
    caller drops it. Every pubkey present therefore carries an exact count,
    never a truncated one — which is why `verifiedFollowerCount` is always a
    real number on the wire.
    """
    resolved: dict[str, int] = {}
    by_cap: dict[int, list[str]] = defaultdict(list)

    for row in candidates:
        stored = safe_int(row.get("stored_followers"))
        if stored is not None:
            resolved[row["pubkey"]] = stored
        else:
            by_cap[follower_count_cap_for(int(row["verified_reporters"]))].append(
                row["pubkey"]
            )

    if by_cap:
        logger.info(
            "network_alerts: %d candidate(s) missing trusted_followers; "
            "counting from the graph across %d capped bucket(s)",
            sum(len(v) for v in by_cap.values()),
            len(by_cap),
        )

    for cap, pubkeys in by_cap.items():
        counts = await count_above_cutoff_followers_capped(
            session=session,
            pubkeys=pubkeys,
            influence_key=influence_key,
            cutoff=cutoffs.verified_line,
            cap=cap,
        )
        for pubkey in pubkeys:
            count = counts.get(pubkey)
            # No row back, or one that hit the cap (true count >= cap): either
            # way the row can't clear its threshold, so leave it out. Defaulting
            # to 0 here would pin N at its floor and alert on it — see the
            # module docstring.
            if count is not None and count < cap:
                resolved[pubkey] = count

    return resolved


def _to_item(
    row: dict, verified_followers: int, verified_muters: int
) -> NetworkAlertItem:
    return NetworkAlertItem(
        pubkey=row["pubkey"],
        influence=safe_float(row.get("influence")),
        hops=safe_int(row.get("hops")),
        verified_follower_count=verified_followers,
        verified_muter_count=verified_muters,
        verified_reporter_count=int(row["verified_reporters"]),
        reporter_threshold=reporter_threshold_for(verified_followers),
    )


async def get_network_alerts_for_observer(
    observer: str,
    cutoffs: VerifiedCutoffs,
    limit: int = DEFAULT_ALERT_LIMIT,
) -> NetworkAlertsData:
    """Both alert sections from the observer's perspective.

    `observer` is a resolved hex pubkey and `cutoffs` their saved preset, settled
    by `resolve_alert_observer` / `get_alert_cutoffs` before the handler runs —
    which is what keeps this function Neo4j-only. The observer is whoever the
    caller names: the front end passes the House pubkey when it wants the House
    point of view, so this service has no notion of who the House is.

    The muter count rides its own cutoff, not the follower line.
    """
    influence_key = f"influence_{observer}"

    async with neo4j_driver.session() as session:
        candidates = await get_network_alert_candidates(
            session=session,
            observer_pubkey=observer,
            influence_key=influence_key,
            hops_key=f"hops_{observer}",
            trusted_followers_key=f"trusted_followers_{observer}",
            trusted_reporters_key=f"trusted_reporters_{observer}",
            cutoff=cutoffs.verified_line,
        )
        if len(candidates) >= MAX_ALERT_CANDIDATES:
            # Never truncate quietly — a capped candidate set means the sections
            # below are incomplete in a way the truncation flags don't express.
            logger.warning(
                "network_alerts: candidate ceiling (%d) reached for observer %s; "
                "results may be incomplete",
                MAX_ALERT_CANDIDATES,
                observer[:12],
            )

        follower_counts = await _resolve_follower_counts(
            session, candidates, influence_key, cutoffs
        )

        # Threshold test. Candidates missing from follower_counts are already
        # known not to clear it — see _resolve_follower_counts.
        alerts = [
            (row, follower_counts[row["pubkey"]])
            for row in candidates
            if row["pubkey"] in follower_counts
            and int(row["verified_reporters"])
            > reporter_threshold_for(follower_counts[row["pubkey"]])
        ]

        # The `is_direct` split is what makes the sections disjoint, so a pubkey
        # qualifying for both is reported once, under direct follows.
        direct = [(r, f) for r, f in alerts if r["is_direct"]]
        extended = [(r, f) for r, f in alerts if not r["is_direct"]]

        direct.sort(key=lambda rf: (-int(rf[0]["verified_reporters"]), rf[0]["pubkey"]))
        extended.sort(
            key=lambda rf: (-(safe_float(rf[0]["influence"]) or 0.0), rf[0]["pubkey"])
        )

        direct_page, extended_page = direct[:limit], extended[:limit]

        muters = await count_verified_muters(
            session=session,
            pubkeys=[r["pubkey"] for r, _ in direct_page + extended_page],
            influence_key=influence_key,
            verified_threshold=cutoffs.muter,
        )

    return NetworkAlertsData(
        observer_pubkey=observer,
        direct_follows=[
            _to_item(r, f, muters.get(r["pubkey"], 0)) for r, f in direct_page
        ],
        extended_network=[
            _to_item(r, f, muters.get(r["pubkey"], 0)) for r, f in extended_page
        ],
        direct_follows_truncated=len(direct) > limit,
        extended_network_truncated=len(extended) > limit,
    )
