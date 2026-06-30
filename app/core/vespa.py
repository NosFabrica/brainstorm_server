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
import os

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

# In-flight feed concurrency = number of SIMULTANEOUS connections, because the
# Vespa cleartext endpoint serves HTTP/1.1 (h2c is not enabled, and httpx only
# negotiates HTTP/2 via ALPN over TLS — see _HTTP2 below). There is NO stream
# multiplexing, so each concurrent op needs its own TCP connection. Firing a big
# burst of COLD connects stampedes Vespa's acceptor and trips the connect timeout:
# measured locally, 128 cold connects dropped ~25-46% of ops; 32 dropped 0. Keep
# this modest — within a run connections are reused (keepalive), so 32 pipelines
# fine. Env-overridable (VESPA_FEED_CONCURRENCY), a values-file change, no rebuild.
# If the server is scaled out, the aggregate across feeders is
# replicas * uvicorn_workers * this — lower it accordingly.
_BATCH_CONCURRENCY = int(os.getenv("VESPA_FEED_CONCURRENCY", "32"))

# HTTP/2 would let many ops multiplex over one connection (no cold-connect herd),
# but httpx only negotiates h2 via ALPN over TLS; against the plaintext http://
# Vespa endpoint it always uses HTTP/1.1 (confirmed empirically). So this toggle
# is effectively a no-op until Vespa is served over TLS (https). Harmless — httpx
# falls back to HTTP/1.1 over cleartext regardless of this flag.
_HTTP2 = os.getenv("VESPA_HTTP2", "true").lower() not in ("0", "false", "no")

# Cold-connect failures (the HTTP/1.1 herd above) are retried rather than dropped —
# the retry lands on a now-warm connection and succeeds. This is what makes
# feeding lossless. (Upserts/removes stay best-effort overall — a still-failing op
# is logged, never raised, and the next GrapeRank run re-feeds it.)
_CONNECT_RETRIES = int(os.getenv("VESPA_CONNECT_RETRIES", "3"))
_RETRYABLE = (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)


# ---------------------------------------------------------------------------
# shared async client (kept open for the lifetime of the process so connections
# stay pooled and we don't pay TCP+TLS handshake on every request)
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


def _build_client(http2: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=http2,
        # Short connect timeout: a healthy connect is <100ms, so a cold connect
        # that stalls past this is abandoned fast and retried (_put_with_retry)
        # onto a warm connection instead of blocking. Measured: connect=1 + conc=32
        # + retries=3 = 256 ops lossless in ~0.33s. Read/write stays generous.
        timeout=httpx.Timeout(30.0, connect=1.0),
        limits=httpx.Limits(
            max_connections=200,
            max_keepalive_connections=100,
        ),
    )


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        if _HTTP2:
            try:
                _client = _build_client(http2=True)
            except ImportError:
                # httpx[http2]/h2 not installed yet (image not rebuilt) —
                # feeding still works over HTTP/1.1, just slower.
                logger.warning(
                    "Vespa HTTP/2 requested but h2 is unavailable "
                    "(install httpx[http2]); falling back to HTTP/1.1"
                )
                _client = _build_client(http2=False)
        else:
            _client = _build_client(http2=False)
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
    return f"{settings.vespa_url}/document/v1/{NAMESPACE}/{DOCTYPE}/docid/{pubkey}"


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


async def _put_with_retry(
    url: str, *, params: dict | None = None, json: dict | None = None
) -> httpx.Response:
    """PUT, retrying only connect-level failures. 4xx/5xx responses are returned
    as-is (they carry Vespa's reason and are handled by `_raise_with_context`)."""
    client = _get_client()
    for attempt in range(_CONNECT_RETRIES + 1):
        try:
            return await client.put(url, params=params, json=json)
        except _RETRYABLE:
            if attempt == _CONNECT_RETRIES:
                raise
            await asyncio.sleep(0.1 * (attempt + 1))
    raise AssertionError("unreachable")  # loop either returns or raises


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


def _read_quality_score(fields: dict, observer: str) -> int | None:
    """Extract one observer's cell from a doc's `quality_scores` tensor.
    Tolerates Vespa's `{"cells": [{"address": {"user": ...}, "value": ...}]}`
    long form and the `{"<observer>": value}` short form."""
    tensor = (fields or {}).get("quality_scores")
    if not tensor:
        return None
    cells = tensor.get("cells") if isinstance(tensor, dict) else None
    if isinstance(cells, list):
        for cell in cells:
            if cell.get("address", {}).get("user") == observer:
                return int(cell["value"])
        return None
    if isinstance(tensor, dict):
        value = tensor.get(observer)
        return int(value) if value is not None else None
    return None


async def get_observer_score(pubkey: str, observer: str) -> int | None:
    """The observer's score cell on `pubkey`'s doc, or None if the doc/cell is
    absent. Used by the admin reconcile to diff Vespa against the desired state."""
    fields = await get_document(pubkey)
    if fields is None:
        return None
    return _read_quality_score(fields, observer)


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
    r = await _put_with_retry(_doc_url(pubkey), params={"create": "true"}, json=body)
    _raise_with_context("upsert_profile", pubkey, body, r)


async def upsert_score(pubkey: str, observer: str, score: int) -> None:
    """Set the score for `observer` on the doc identified by `pubkey`.

    `add` upserts the cell — inserts a new one or replaces the existing one
    for that observer.
    """
    body = {
        "fields": {
            "quality_scores": {
                "add": {"cells": [{"address": {"user": observer}, "value": int(score)}]}
            }
        }
    }
    r = await _put_with_retry(_doc_url(pubkey), params={"create": "true"}, json=body)
    _raise_with_context("upsert_score", pubkey, body, r)


async def remove_score(pubkey: str, observer: str) -> None:
    """Remove the observer's score from the doc's tensor."""
    body = {
        "fields": {"quality_scores": {"remove": {"addresses": [{"user": observer}]}}}
    }
    r = await _put_with_retry(_doc_url(pubkey), json=body)
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

    # No pre-warm: under HTTP/1.1 it only warms one of the ~conc connections, so it
    # never prevented the cold-connect herd. Modest VESPA_FEED_CONCURRENCY + the
    # connect-timeout/retry in _put_with_retry handle that (measured lossless).
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
            logger.warning(f"... and {len(failed) - 5} more vespa score-batch failures")
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
    """Per-word fuzzy budget — 0 for very short words, up to 2 for longer ones."""
    return 0 if len(word) < 3 else (1 if len(word) < 6 else 2)


def _field_clauses(field: str, var: str, max_edits: int) -> list[str]:
    parts = [
        f'({{defaultIndex:"{field}"}}userInput({var}))',
        f'({{defaultIndex:"{field}",prefix:true}}userInput({var}))',
    ]
    if max_edits > 0:
        parts.append(
            f'({{defaultIndex:"{field}",fuzzy:{{maxEditDistance:{max_edits},prefixLength:1}}}}userInput({var}))'
        )
    return parts


def _word_group(var: str, literal: str, with_grams: bool = True) -> str:
    """All match clauses for one query word across name/display_name/about + grams."""
    me = _word_max_edits(literal)
    clauses: list[str] = []
    for field in ("name", "display_name", "about"):
        clauses += _field_clauses(field, var, me)
    if with_grams:
        for gram_field in ("name_gram", "display_name_gram"):
            gc = _gram_clause(literal, gram_field)
            if gc:
                clauses.append(gc)
        agc = _about_gram_clause_for_word(literal)
        if agc:
            clauses.append(agc)
    return "(" + " or ".join(clauses) + ")"


def _build_yql(words: list[str], joined: str | None) -> str:
    """Per-word groups OR'd together, plus an optional joined-CamelCase variant
    (whole-token only) so a query like 'vitor pamplona' still hits a doc named
    'VitorPamplona'."""
    parts = [_word_group(f"@w{i}", w) for i, w in enumerate(words[:MAX_QUERY_WORDS])]
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
            if (h.get("fields", {}).get("matchfeatures", {}).get("user_score", 0) or 0)
            > 0
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
