"""Vespa client + search helpers.

The schema lives in the Vespa application package (see
brainstorm_one_click_deployment/vespa-app) and uses a sparse tensor
(`quality_scores`, keyed by observer pubkey) so each observer's ranking lives
in its own cell of that tensor.

The search function uses the `text_relevance` rank profile (PURE TEXT — trust
is not blended in; see docs/search-vs-tapestry.md §6):
- name + display_name + about are searched
- about partial matches via `about_gram` (trigrams)
- single combined query + Vespa over-fetch for WAND-resistance
The NIP-50 relay adds trust via the rank_* profiles (defaulting to rank_desc).
"""
import asyncio
import json

import httpx

from app.core.config import settings
from app.core.loggr import loggr
from app.core.vespa_query import MAX_QUERY_WORDS, build_query

logger = loggr.get_logger(__name__)

NAMESPACE = "doc"
DOCTYPE = "doc"

# Profile fields that the kind-0 Nostr event populates.
PROFILE_FIELDS = (
    "name",
    "display_name",
    "username",
    "about",
    "picture",
    "banner",
    "nip05",
    "lud06",
    "lud16",
    "website",
)

# Maps the doc.sd match_quality() name tier (1..4) to a human label surfaced in
# the search response as `_match_tier`. Tier 0 resolves to "affiliation" (bio or
# website domain match) or "gram" (trigram/recall noise). See §10 / §11.
_MATCH_TIERS = {4: "exact", 3: "prefix", 2: "1-typo", 1: "2-typo"}

# Rank-profile names defined in the Vespa schema (doc.sd). The DEFAULT is
# `sort_followers`: text match, filter rank >= query(min_rank), sort by the
# observer's verified-follower count (popular-first). The other profiles are
# the configurable sort alternates (rank / text). All gate on query(min_rank)
# except text_relevance. See docs/search-vs-tapestry.md §8.
DEFAULT_RANK_PROFILE = "sort_followers"
RANK_PROFILE_SORT_FOLLOWERS = "sort_followers"
RANK_PROFILE_FILTERED = "rank_filtered"
RANK_PROFILE_SORT_DESC = "rank_desc"
RANK_PROFILE_SORT_ASC = "rank_asc"
RANK_PROFILE_TEXT = "text_relevance"

# Maps a public `sort=` value (byText) / NIP-50 sort metric to a rank profile.
# `followers` is the default; `rank` orders by trust; `text` is pure relevance.
SORT_PROFILES = {
    "followers": RANK_PROFILE_SORT_FOLLOWERS,
    "rank": RANK_PROFILE_SORT_DESC,
    "text": RANK_PROFILE_FILTERED,
}

# Default query-time trust filter: exclude rank < this (rank = influence*100).
# Product default is "exclude based on rank 2" (docs/search-vs-tapestry.md §8.1);
# configurable per request. The sort_followers/rank_* profiles gate on
# query(min_rank); text_relevance ignores it.
DEFAULT_MIN_RANK = 2.0

# Bounded concurrency for batch score upserts. Vespa's container can handle a
# lot more than this — the limit is mostly to keep retry/backoff predictable.
_BATCH_CONCURRENCY = 32


# ---------------------------------------------------------------------------
# shared async client (kept open for the lifetime of the process so connections
# stay pooled and we don't pay TCP+TLS handshake on every request)
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(
                max_connections=200,
                max_keepalive_connections=100,
            ),
        )
    return _client


async def aclose() -> None:
    """Close the shared client. Call this from the FastAPI lifespan shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# document URLs
# ---------------------------------------------------------------------------
def _doc_url(pubkey: str) -> str:
    return (
        f"{settings.vespa_url}/document/v1/{NAMESPACE}/{DOCTYPE}/docid/{pubkey}"
    )


def _raise_with_context(
    op: str, pubkey: str, body: dict, response: httpx.Response
) -> None:
    """Log Vespa's response body + the request we sent, then raise.

    Vespa's 400/409/5xx bodies carry the actual reason (field-shape mismatch,
    unknown field, schema rejection). httpx's stock HTTPStatusError doesn't
    include them, so without this we'd see only "Client error '400 Bad Request'"
    in the logs. Truncate to keep one bad doc from flooding the log.
    """
    if response.status_code < 400:
        return
    logger.error(
        "vespa %s rejected pubkey=%s status=%d body=%s sent=%s",
        op,
        pubkey,
        response.status_code,
        response.text[:600],
        json.dumps(body)[:600],
    )
    response.raise_for_status()


# ---------------------------------------------------------------------------
# document CRUD
# ---------------------------------------------------------------------------
async def get_document(pubkey: str) -> dict | None:
    """Fetch a document's fields by pubkey; None if not present."""
    r = await _get_client().get(_doc_url(pubkey))
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("fields")


# Vespa string fields reject control characters — a kind-0 whose name is a
# literal NUL (\x00) gets a 400 ("illegal code point 0x0"). Strip the C0 control set
# (and DEL) except tab/newline/CR so the profile indexes cleaned instead of
# failing the upsert. Applies to BOTH the live ingest path and the kind-0
# re-feed backfill (scripts/refeed_kind0_to_vespa.py).
_BAD_CHARS: dict[int, None] = {
    c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)
}
_BAD_CHARS[0x7F] = None


def _clean(value: str) -> str:
    """Drop control characters Vespa string fields reject."""
    return value.translate(_BAD_CHARS)


async def upsert_profile(pubkey: str, profile: dict) -> None:
    """Partial-update profile fields for a doc, creating it if absent.

    For every standard kind-0 field we either assign the provided value or
    clear it with an empty string when the new event doesn't include it, so
    the prior value is replaced rather than left stale. String values are
    stripped of control characters Vespa rejects (see `_clean`).
    """
    fields_payload: dict = {"pubkey": {"assign": pubkey}}
    for f in PROFILE_FIELDS:
        v = profile.get(f)
        # Vespa string fields don't support null — clearing is "" (the schema's
        # default). For strings that arrived as ints/dicts we coerce to str.
        if v is None:
            fields_payload[f] = {"assign": ""}
        elif isinstance(v, str):
            fields_payload[f] = {"assign": _clean(v)}
        else:
            fields_payload[f] = {"assign": _clean(str(v))}

    body = {"fields": fields_payload}
    # PUT (not POST) is Vespa's partial-update verb: assign/add/remove ops live
    # under PUT, while POST is full-doc replace with direct values. `?create=true`
    # creates the doc from the partial update ops if it doesn't exist yet,
    # which preserves the quality_scores tensor across profile updates.
    r = await _get_client().put(
        _doc_url(pubkey), params={"create": "true"}, json=body
    )
    _raise_with_context("upsert_profile", pubkey, body, r)


async def upsert_score(
    pubkey: str, observer: str, score: int, followers: int = 0
) -> None:
    """Set the observer's score + verified-follower count on the doc.

    `score` is the rank (influence*100, 0..100) → `quality_scores` tensor;
    `followers` is the verified-follower count (scorecard.trusted_followers) →
    `follower_counts` tensor. Both `add` ops upsert the observer's cell (insert
    or replace). Written in one partial update so the two stay consistent.
    """
    body = {
        "fields": {
            "quality_scores": {
                "add": {
                    "cells": [
                        {"address": {"user": observer}, "value": int(score)}
                    ]
                }
            },
            "follower_counts": {
                "add": {
                    "cells": [
                        {"address": {"user": observer}, "value": float(followers)}
                    ]
                }
            },
        }
    }
    r = await _get_client().put(
        _doc_url(pubkey), params={"create": "true"}, json=body
    )
    _raise_with_context("upsert_score", pubkey, body, r)


async def remove_score(pubkey: str, observer: str) -> None:
    """Remove the observer's score + follower count from the doc's tensors."""
    body = {
        "fields": {
            "quality_scores": {
                "remove": {"addresses": [{"user": observer}]}
            },
            "follower_counts": {
                "remove": {"addresses": [{"user": observer}]}
            },
        }
    }
    r = await _get_client().put(_doc_url(pubkey), json=body)
    # 404 is fine — nothing to remove if the doc isn't there yet.
    if r.status_code == 404:
        return
    _raise_with_context("remove_score", pubkey, body, r)


async def batch_upsert_scores(
    upserts: list[tuple[str, int, int]],
    removes: list[str],
    observer: str,
) -> tuple[int, int]:
    """Run many score upserts + removes concurrently against Vespa.

    `upserts` is a list of (pubkey, score, followers) tuples; `removes` is a
    list of pubkeys whose score for `observer` should be deleted. Returns
    (n_success, n_failed). Individual failures are logged but never raised — the
    caller treats scores as best-effort search mirror, not source of truth.
    """
    if not upserts and not removes:
        return 0, 0

    sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

    async def _do_upsert(pubkey: str, score: int, followers: int) -> None:
        async with sem:
            await upsert_score(pubkey, observer, score, followers)

    async def _do_remove(pubkey: str) -> None:
        async with sem:
            await remove_score(pubkey, observer)

    tasks: list = [_do_upsert(pk, sc, fc) for pk, sc, fc in upserts]
    tasks += [_do_remove(pk) for pk in removes]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    failed = [r for r in results if isinstance(r, BaseException)]
    if failed:
        # Log the first few exceptions verbatim; collapse the rest into a count.
        for exc in failed[:5]:
            logger.warning(f"vespa score-batch op failed: {exc!r}")
        if len(failed) > 5:
            logger.warning(
                f"... and {len(failed) - 5} more vespa score-batch failures"
            )
    return len(results) - len(failed), len(failed)


# ---------------------------------------------------------------------------
# YQL builders live in app/core/vespa_query.py (stdlib-only) so diagnostic
# tooling (scripts/analyze_pubkey.py) can build the EXACT same query without
# importing settings/httpx. search() below calls build_query() from there.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
async def search(
    query_text: str,
    user_pubkey: str,
    hits: int = 100,
    include_zero_score_results: bool = True,
    *,
    ranking_profile: str | None = None,
    min_rank: float | None = None,
) -> list[dict]:
    """Multi-field search against a quality-score rank profile.

    `user_pubkey` is the observer perspective whose quality_score is used for
    ranking. Returns a list of dicts, each merging the doc's stored fields with
    `_relevance` and a `_quality_score` (the observer's score for that doc).

    `ranking_profile` selects the Vespa rank profile (defaults to
    `DEFAULT_RANK_PROFILE`); pass one of the `RANK_PROFILE_*` constants to order
    by / filter on the observer's quality score. `min_rank`, when set, is passed
    as `query(min_rank)` so the rank_* profiles drop hits whose quality score is
    below it (the NIP-50 `filter:rank:gte/gt` push-down). Both are no-ops on the
    default profile. See docs/search-precision-and-filtering.md (Problem 2).
    """
    # Build the YQL + per-word params (shared with scripts/analyze_pubkey.py).
    words, yql, word_params = build_query(query_text)
    shortest = min((len(w) for w in words), default=len(query_text))
    # Trigrams are a recall safety-net + tie-breaker, not a primary ranker.
    # Keep them meaningful for very short queries (where token matching barely
    # fires) but well below an exact token match otherwise. The schema also
    # caps the per-field gram contribution via query(gram_cap). See Problem 1.
    w_gram = 8.0 if shortest <= 3 else 2.0

    vespa_hits = max(hits, 20)
    if not include_zero_score_results:
        vespa_hits = max(hits * 5, 100)
    vespa_hits = min(vespa_hits, 400)  # Vespa default max-hits

    params = {
        "yql": yql,
        "ranking": ranking_profile or DEFAULT_RANK_PROFILE,
        "ranking.features.query(user_q)": "{" + user_pubkey + ":1.0}",
        "ranking.features.query(w_gram)": w_gram,
        "ranking.features.query(w_about)": 0.5,
        "ranking.features.query(w_about_bonus)": 0.0,
        "hits": vespa_hits,
    }
    if min_rank is not None:
        params["ranking.features.query(min_rank)"] = min_rank
    params.update(word_params)

    r = await _get_client().get(f"{settings.vespa_url}/search/", params=params)
    r.raise_for_status()
    data = r.json()

    children = data.get("root", {}).get("children", [])
    if not include_zero_score_results:
        children = [
            h
            for h in children
            if (h.get("fields", {}).get("matchfeatures", {}).get("user_score", 0) or 0) > 0
        ]
    children = children[:hits]

    out: list[dict] = []
    for h in children:
        fields = dict(h.get("fields", {}))
        mf = fields.pop("matchfeatures", None) or {}
        fields["_relevance"] = h.get("relevance")
        fields["_quality_score"] = mf.get("user_score")
        # verified_followers is only in match-features for the sort_followers
        # profile; other profiles leave it None.
        fields["_followers"] = mf.get("verified_followers")
        # match tier (doc.sd, §11): "name" (name/display/username/nip05 token
        # match) > "affiliation" (about/website match) > "gram" (trigram/recall
        # noise). The finer exact>prefix>typo name ladder (match_quality) is
        # DEFERRED — itemRawScore doesn't populate for text terms, so it's always
        # 0; we fall back to the reliable matchCount-based signals. `mf` is empty
        # on the npub/hex direct-fetch path (no ranking ran).
        mq = mf.get("match_quality")
        htm = mf.get("has_token_match")
        am = mf.get("affiliation_match")
        fields["_match_quality"] = mq
        if mf:
            if mq and int(mq) > 0:
                fields["_match_tier"] = _MATCH_TIERS.get(int(mq), str(mq))
            elif htm:
                fields["_match_tier"] = "name"
            elif am:
                fields["_match_tier"] = "affiliation"
            else:
                fields["_match_tier"] = "gram"
        out.append(fields)
    return out
