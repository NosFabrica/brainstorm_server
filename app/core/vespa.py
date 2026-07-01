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

# How many query words we label / parametrize at most.
MAX_QUERY_WORDS = 6

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
# YQL builders (ported from the search_quality prototype)
# ---------------------------------------------------------------------------
def _gram_clause(text: str, gram_field: str, gram_size: int = 3) -> str:
    """OR of every trigram in `text` against `gram_field`."""
    grams = set()
    for word in text.lower().split():
        for i in range(max(1, len(word) - gram_size + 1)):
            g = word[i : i + gram_size]
            if len(g) == gram_size and g.isalnum():
                grams.add(g)
    if not grams:
        return ""
    return (
        "(" + " or ".join(f'{gram_field} contains "{g}"' for g in sorted(grams)) + ")"
    )


def _about_gram_clause_for_word(word: str, gram_size: int = 3) -> str:
    """AND of one word's trigrams against `about_gram` (discriminative)."""
    grams = [
        word[i : i + gram_size]
        for i in range(len(word) - gram_size + 1)
        if word[i : i + gram_size].isalnum()
        and len(word[i : i + gram_size]) == gram_size
    ]
    if not grams:
        return ""
    return "(" + " and ".join(f'about_gram contains "{g}"' for g in grams) + ")"


def _word_max_edits(word: str) -> int:
    """Per-word fuzzy budget, length-gated like Meilisearch: <4 → exact/prefix
    only, ≥4 → 1 edit, ≥9 → 2 edits.

    Two edits is safe now that the rank profile tiers exact > prefix > 1-typo >
    2-typo (match_quality in doc.sd, via the labels added in `_field_clauses`):
    a 2-edit neighbour lands in the lowest match tier instead of scoring like an
    exact hit, which is what made wider fuzzy "suffer" before. See
    docs/search-precision-and-filtering.md (Problem 1) and the §10 typo
    deep-dive in docs/search-vs-tapestry.md.
    """
    if len(word) >= 9:
        return 2
    if len(word) >= 4:
        return 1
    return 0


# Field roles for match labeling (consumed by match_quality / affiliation_match
# in doc.sd via itemRawScore):
#   primary     — name/display_name/username/nip05 → exact>prefix>1-typo>2-typo
#   affiliation — bio + website → only the EXACT-token match is labeled
#                 (mtch_affil), driving the affiliation tier (below name matches,
#                 above gram noise). "related to X" signals: a bio mentioning X,
#                 or an account whose own website domain is X.
#   recall      — lud16 → unlabeled, pure recall
# NB website is a URL: default linguistics splits "odell.com" -> [odell, com]
# (exact "odell" hits) but "citadeldispatch.com" -> [citadeldispatch, com]
# (exact "citadel" misses — would need prefix). See §11.
_PRIMARY_FIELDS = ("name", "display_name", "username", "nip05")
_AFFILIATION_FIELDS = ("about", "website")


def _field_role(field: str) -> str:
    if field in _PRIMARY_FIELDS:
        return "primary"
    if field in _AFFILIATION_FIELDS:
        return "affiliation"
    return "recall"


def _field_clauses(field: str, var: str, max_edits: int, role: str) -> list[str]:
    """Match clauses for one (field, word): exact, prefix, and up to two fuzzy
    tiers. Labels depend on the field `role` so the rank profile can build the
    exact>prefix>1-typo>2-typo name ladder (match_quality) AND the affiliation
    tier (affiliation_match), both via itemRawScore(). Recall fields stay
    unlabeled so a hit there can't jump tiers. See docs §10 / §11.
    """
    def _ann(extra: list[str], label: str | None) -> str:
        ann = [f'defaultIndex:"{field}"'] + extra
        if label:
            ann.append(f'label:"{label}"')
        return "{" + ",".join(ann) + "}"

    ex_label = {"primary": "mtch_exact", "affiliation": "mtch_affil"}.get(role)
    pf_label = "mtch_prefix" if role == "primary" else None
    clauses = [
        f"({_ann([], ex_label)}userInput({var}))",
        f"({_ann(['prefix:true'], pf_label)}userInput({var}))",
    ]
    # prefixLength:2 — the first two characters must match exactly, so a typo in
    # char >=3 is corrected but a wrong first/second letter no longer drags in
    # unrelated names. Separate fz1/fz2 labels so 1-typo outranks 2-typo.
    if max_edits >= 1:
        fz1 = "mtch_fz1" if role == "primary" else None
        clauses.append(
            f"({_ann(['fuzzy:{maxEditDistance:1,prefixLength:2}'], fz1)}userInput({var}))"
        )
    if max_edits >= 2:
        fz2 = "mtch_fz2" if role == "primary" else None
        clauses.append(
            f"({_ann(['fuzzy:{maxEditDistance:2,prefixLength:2}'], fz2)}userInput({var}))"
        )
    return clauses


def _word_group(var: str, literal: str, with_grams: bool = True) -> str:
    """All match clauses for one query word across the searchable fields + grams.

    name/display_name/about are the primary text signal (and feed the rank
    profile's text functions). username/nip05 also feed the match-quality tier
    (match_quality in doc.sd). lud16/website are recall-only (P1). See
    docs/search-vs-tapestry.md §8.4.
    """
    me = _word_max_edits(literal)
    clauses: list[str] = []
    for field in ("name", "display_name", "about", "username", "nip05", "lud16", "website"):
        clauses += _field_clauses(field, var, me, _field_role(field))
    if with_grams:
        for gram_field in ("name_gram", "display_name_gram"):
            gc = _gram_clause(literal, gram_field)
            if gc:
                clauses.append(gc)
        agc = _about_gram_clause_for_word(literal)
        if agc:
            clauses.append(agc)
    return "(" + " or ".join(clauses) + ")"


def _build_yql(words: list[str], joined: str | None, pairs: list[str]) -> str:
    """Per-word groups OR'd together, plus an optional joined-CamelCase variant
    (whole-token only) so a query like 'vitor pamplona' still hits a doc named
    'VitorPamplona', plus adjacent-pair concatenations for >2-word queries."""
    parts = [
        _word_group(f"@w{i}", w)
        for i, w in enumerate(words[:MAX_QUERY_WORDS])
    ]
    if joined:
        parts.append(_word_group("@wj", joined, with_grams=False))
    for i, _ in enumerate(pairs):
        parts.append(_word_group(f"@wp{i}", pairs[i], with_grams=False))
    return f"select * from doc where {' or '.join(parts)}"


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
    words = query_text.split()[:MAX_QUERY_WORDS]
    joined = "".join(words) if len(words) >= 2 else None
    # Adjacent-pair concatenations ("nos fabrica" -> "nosfabrica") for queries
    # with >2 words, where the whole-string join (`joined`) wouldn't match a
    # 2-token name. 2-word queries are already covered by `joined`. Whole-token
    # only (no grams). See the §10 typo deep-dive (word-boundary handling).
    pairs = (
        ["".join(words[i:i + 2]) for i in range(len(words) - 1)]
        if len(words) >= 3
        else []
    )
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
        "yql": _build_yql(words, joined, pairs),
        "ranking": ranking_profile or DEFAULT_RANK_PROFILE,
        "ranking.features.query(user_q)": "{" + user_pubkey + ":1.0}",
        "ranking.features.query(w_gram)": w_gram,
        "ranking.features.query(w_about)": 0.5,
        "ranking.features.query(w_about_bonus)": 0.0,
        "hits": vespa_hits,
    }
    if min_rank is not None:
        params["ranking.features.query(min_rank)"] = min_rank
    for i, w in enumerate(words):
        params[f"w{i}"] = w
    if joined:
        params["wj"] = joined
    for i, p in enumerate(pairs):
        params[f"wp{i}"] = p

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
        # match tier (doc.sd, §10/§11): name ladder exact>prefix>1-typo>2-typo,
        # then "affiliation" (bio/website match) above "gram" (trigram/recall
        # noise). Surfaced so the team can eyeball why a hit ranked where it did.
        # None on the npub/hex direct-fetch path.
        mq = mf.get("match_quality")
        am = mf.get("affiliation_match")
        fields["_match_quality"] = mq
        if mq is not None:
            if int(mq) > 0:
                fields["_match_tier"] = _MATCH_TIERS.get(int(mq), str(mq))
            elif am:
                fields["_match_tier"] = "affiliation"
            else:
                fields["_match_tier"] = "gram"
        out.append(fields)
    return out
