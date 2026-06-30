"""Vespa client + search helpers.

The schema lives in the Vespa application package (see
brainstorm_one_click_deployment/vespa-app) and uses a sparse tensor
(`quality_scores`, keyed by observer pubkey) so each observer's ranking lives
in its own cell of that tensor.

The search function uses the `name_and_quality_score_only` rank profile:
- name + display_name + about are searched
- about partial matches via `about_gram` (trigrams)
- single combined query + Vespa over-fetch for WAND-resistance
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


async def upsert_profile(pubkey: str, profile: dict) -> None:
    """Partial-update profile fields for a doc, creating it if absent.

    For every standard kind-0 field we either assign the provided value or
    clear it with an empty string when the new event doesn't include it, so
    the prior value is replaced rather than left stale.
    """
    fields_payload: dict = {"pubkey": {"assign": pubkey}}
    for f in PROFILE_FIELDS:
        v = profile.get(f)
        # Vespa string fields don't support null — clearing is "" (the schema's
        # default). For strings that arrived as ints/dicts we coerce to str.
        if v is None:
            fields_payload[f] = {"assign": ""}
        elif isinstance(v, str):
            fields_payload[f] = {"assign": v}
        else:
            fields_payload[f] = {"assign": str(v)}

    body = {"fields": fields_payload}
    # PUT (not POST) is Vespa's partial-update verb: assign/add/remove ops live
    # under PUT, while POST is full-doc replace with direct values. `?create=true`
    # creates the doc from the partial update ops if it doesn't exist yet,
    # which preserves the quality_scores tensor across profile updates.
    r = await _get_client().put(
        _doc_url(pubkey), params={"create": "true"}, json=body
    )
    _raise_with_context("upsert_profile", pubkey, body, r)


async def upsert_score(pubkey: str, observer: str, score: int) -> None:
    """Set the score for `observer` on the doc identified by `pubkey`.

    `add` upserts the cell — inserts a new one or replaces the existing one
    for that observer.
    """
    body = {
        "fields": {
            "quality_scores": {
                "add": {
                    "cells": [
                        {"address": {"user": observer}, "value": int(score)}
                    ]
                }
            }
        }
    }
    r = await _get_client().put(
        _doc_url(pubkey), params={"create": "true"}, json=body
    )
    _raise_with_context("upsert_score", pubkey, body, r)


async def remove_score(pubkey: str, observer: str) -> None:
    """Remove the observer's score from the doc's tensor."""
    body = {
        "fields": {
            "quality_scores": {
                "remove": {"addresses": [{"user": observer}]}
            }
        }
    }
    r = await _get_client().put(_doc_url(pubkey), json=body)
    # 404 is fine — nothing to remove if the doc isn't there yet.
    if r.status_code == 404:
        return
    _raise_with_context("remove_score", pubkey, body, r)


async def batch_upsert_scores(
    upserts: list[tuple[str, int]],
    removes: list[str],
    observer: str,
) -> tuple[int, int]:
    """Run many score upserts + removes concurrently against Vespa.

    `upserts` is a list of (pubkey, score) tuples; `removes` is a list of
    pubkeys whose score for `observer` should be deleted. Returns (n_success,
    n_failed). Individual failures are logged but never raised — the caller
    treats scores as best-effort search mirror, not source of truth.
    """
    if not upserts and not removes:
        return 0, 0

    sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

    async def _do_upsert(pubkey: str, score: int) -> None:
        async with sem:
            await upsert_score(pubkey, observer, score)

    async def _do_remove(pubkey: str) -> None:
        async with sem:
            await remove_score(pubkey, observer)

    tasks: list = [_do_upsert(pk, sc) for pk, sc in upserts]
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
def _gram_and_clause(word: str, gram_field: str, gram_size: int = 3) -> str:
    """AND of one word's trigrams against a `*_gram` field (discriminative).

    Requiring *every* trigram to be present (rather than any single shared one)
    is what keeps the near-miss long tail out: a doc only matches on grams when
    the whole query word occurs as a substring, mirroring the partial-bio
    behaviour `about_gram` already had ("nosfab" -> "nosfabrica"). Real typos
    are still caught by the fuzzy/prefix `userInput` clauses on the text fields
    themselves, so this only drops the one-shared-trigram noise.
    """
    w = word.lower()
    grams = [
        w[i : i + gram_size]
        for i in range(len(w) - gram_size + 1)
        if len(w[i : i + gram_size]) == gram_size and w[i : i + gram_size].isalnum()
    ]
    if not grams:
        return ""
    return "(" + " and ".join(f'{gram_field} contains "{g}"' for g in grams) + ")"


def _word_max_edits(word: str) -> int:
    """Per-word fuzzy budget. Capped at 1 edit and disabled below 4 chars:
    2-edit fuzzy and short-word fuzzy are the biggest sources of typo noise
    (a 2-edit match on a 6-char word sharing only its first letter pulls in a
    lot of unrelated names)."""
    return 0 if len(word) < 4 else 1


def _field_clauses(field: str, var: str, max_edits: int) -> list[str]:
    parts = [
        f'({{defaultIndex:"{field}"}}userInput({var}))',
        f'({{defaultIndex:"{field}",prefix:true}}userInput({var}))',
    ]
    if max_edits > 0:
        # prefixLength:2 anchors the first two characters so a typo has to be
        # *inside* the word — a near-match that disagrees on the opening letters
        # no longer qualifies, which is most of the fuzzy garbage.
        parts.append(
            f'({{defaultIndex:"{field}",fuzzy:{{maxEditDistance:{max_edits},prefixLength:2}}}}userInput({var}))'
        )
    return parts


def _word_group(var: str, literal: str, with_grams: bool = True) -> str:
    """All match clauses for one query word across name/display_name/about + grams."""
    me = _word_max_edits(literal)
    clauses: list[str] = []
    for field in ("name", "display_name", "about"):
        clauses += _field_clauses(field, var, me)
    if with_grams:
        for gram_field in ("name_gram", "display_name_gram", "about_gram"):
            gc = _gram_and_clause(literal, gram_field)
            if gc:
                clauses.append(gc)
    return "(" + " or ".join(clauses) + ")"


def _build_yql(words: list[str], joined: str | None) -> str:
    """Per-word groups OR'd together, plus an optional joined-CamelCase variant
    (whole-token only) so a query like 'vitor pamplona' still hits a doc named
    'VitorPamplona'."""
    parts = [
        _word_group(f"@w{i}", w)
        for i, w in enumerate(words[:MAX_QUERY_WORDS])
    ]
    if joined:
        parts.append(_word_group("@wj", joined, with_grams=False))
    return f"select * from doc where {' or '.join(parts)}"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
async def search(
    query_text: str,
    user_pubkey: str,
    hits: int = 100,
    include_zero_score_results: bool = True,
) -> list[dict]:
    """Multi-field search using the `name_and_quality_score_only` rank profile.

    `user_pubkey` is the observer perspective whose quality_score is used for
    ranking. Returns a list of dicts, each merging the doc's stored fields with
    `_relevance` and a `_quality_score` (the observer's score for that doc).
    """
    words = query_text.split()[:MAX_QUERY_WORDS]
    joined = "".join(words) if len(words) >= 2 else None
    shortest = min((len(w) for w in words), default=len(query_text))
    w_gram = 20.0 if shortest <= 3 else 5.0

    vespa_hits = max(hits, 20)
    if not include_zero_score_results:
        vespa_hits = max(hits * 5, 100)
    vespa_hits = min(vespa_hits, 400)  # Vespa default max-hits

    params = {
        "yql": _build_yql(words, joined),
        "ranking": "name_and_quality_score_only",
        "ranking.features.query(user_q)": "{" + user_pubkey + ":1.0}",
        "ranking.features.query(w_gram)": w_gram,
        "ranking.features.query(w_about)": 0.5,
        "ranking.features.query(w_about_bonus)": 0.0,
        "hits": vespa_hits,
    }
    for i, w in enumerate(words):
        params[f"w{i}"] = w
    if joined:
        params["wj"] = joined

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
        out.append(fields)
    return out
