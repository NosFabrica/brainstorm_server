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
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor

import httpx

from app.core import vespa_feed
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

# Total in-flight feed concurrency (env VESPA_FEED_CONCURRENCY). Split across
# workers on the sharded path (per-worker = _BATCH_CONCURRENCY // _FEED_WORKERS),
# used whole in-process. Under h2c these multiplex over a handful of connections;
# the single Vespa content node is the ceiling — past ~128 more in-flight queues.
_BATCH_CONCURRENCY = int(os.getenv("VESPA_FEED_CONCURRENCY", "32"))

# Large batches shard the feed across worker processes: one asyncio loop is
# GIL-bound to ~1 core, but the single Vespa content node has headroom. Workers =
# available cores; the _BATCH_CONCURRENCY budget splits evenly across them. Small
# batches stay in-process (spawn cost not worth it).
_FEED_WORKERS = os.cpu_count() or 1
_FEED_PARALLEL_THRESHOLD = 5000

# HTTP/2 cleartext (h2c). Vespa serves h2c by default and httpx speaks it via
# prior-knowledge once http1 is disabled (see _build_client) — one connection
# multiplexes many partial-update streams (~3x vs HTTP/1.1). Falls back to
# HTTP/1.1 if the h2 package is missing (see _get_client).
_HTTP2 = os.getenv("VESPA_HTTP2", "true").lower() not in ("0", "false", "no")

# Transient connect failures are retried rather than dropped, so feeding is
# lossless. (Upserts/removes stay best-effort overall — a still-failing op is
# logged, never raised, and the next GrapeRank run re-feeds it.)
_CONNECT_RETRIES = int(os.getenv("VESPA_CONNECT_RETRIES", "3"))
_RETRYABLE = (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)

# Large batches (TA score mirror, kind-0 refeed) can be fed by the official JVM
# `vespa-feed-client` subprocess instead of our httpx process-pool. Measured
# ~2.5x throughput (the Python client is CPU-bound; the JVM feeder isn't — same
# h2c wire, far less per-op overhead). Auto-enabled when the JAR is present (the
# image ships a JRE + the JAR); the live-profile trickle stays on the httpx path.
_FEEDER_JAR = os.getenv("VESPA_FEEDER_JAR", "/opt/vespa-feed-client.jar")
_FEEDER_ENABLED = os.path.exists(_FEEDER_JAR)
_FEEDER_THRESHOLD = 5000  # ops; below this the httpx path wins (JVM startup not worth it)
# Optional --connections override; feed-client auto-tunes when unset.
_FEEDER_CONNECTIONS = os.getenv("VESPA_FEEDER_CONNECTIONS")


# ---------------------------------------------------------------------------
# shared async client (kept open for the lifetime of the process so connections
# stay pooled and we don't pay TCP+TLS handshake on every request)
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


def _build_client(http2: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        # h2c (HTTP/2 cleartext, prior-knowledge): httpx only uses HTTP/2 over
        # plaintext when http1 is disabled — with http1 left on it negotiates
        # HTTP/1.1. Vespa serves h2c by default, so one connection multiplexes
        # many partial-update streams (measured ~3x vs HTTP/1.1). Requires h2.
        http1=not http2,
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
    r = await _put_with_retry(
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
    body = vespa_feed.upsert_body(observer, score, followers)
    r = await _put_with_retry(
        _doc_url(pubkey), params={"create": "true"}, json=body
    )
    _raise_with_context("upsert_score", pubkey, body, r)


async def remove_score(pubkey: str, observer: str) -> None:
    """Remove the observer's score + follower count from the doc's tensors."""
    body = vespa_feed.remove_body(observer)
    r = await _put_with_retry(_doc_url(pubkey), json=body)
    # 404 is fine — nothing to remove if the doc isn't there yet.
    if r.status_code == 404:
        return
    _raise_with_context("remove_score", pubkey, body, r)


def _feed_plan(total: int) -> tuple[int, int]:
    """(n_shards, per_shard_concurrency) for a batch of `total` ops.

    In-process (1 shard) at or below the threshold. Above it, scale shards with
    size — double while each shard still holds >= threshold ops — power-of-2 so
    the concurrency budget splits evenly, capped at available cores. So ~5k-20k =
    2 shards @ 64, ~20k-40k = 4 @ 32, ~40k-80k = 8 @ 16, larger = 16 @ 8, …
    """
    if total <= _FEED_PARALLEL_THRESHOLD:
        return 1, _BATCH_CONCURRENCY
    n = 2
    while n * 2 <= _FEED_WORKERS and total // (n * 2) >= _FEED_PARALLEL_THRESHOLD:
        n *= 2
    n = min(n, _FEED_WORKERS)
    return n, max(1, _BATCH_CONCURRENCY // n)


async def batch_upsert_scores(
    upserts: list[tuple[str, int, int]],
    removes: list[str],
    observer: str,
) -> tuple[int, int]:
    """Run many score upserts + removes against Vespa. Returns (n_success,
    n_failed). Individual failures are logged but never raised — scores are a
    best-effort search mirror, not source of truth.

    Small batches run in-process; large ones shard across worker processes so the
    feed isn't bottlenecked on one GIL-bound core (see `_feed_plan`).
    """
    total = len(upserts) + len(removes)
    if total == 0:
        return 0, 0
    # Large batches: hand off to the JVM feeder subprocess if enabled. On any
    # failure (missing JRE/JAR, spawn error, unparseable output) fall through to
    # the httpx path — the mirror stays best-effort, never blocks the run.
    if _FEEDER_ENABLED and total >= _FEEDER_THRESHOLD:
        try:
            return await _batch_upsert_feeder(upserts, removes, observer)
        except Exception as e:
            logger.warning(f"vespa feeder failed ({e!r}); falling back to httpx")
    n_shards, per_shard = _feed_plan(total)
    if n_shards <= 1:
        return await _batch_upsert_inprocess(upserts, removes, observer, per_shard)
    return await _batch_upsert_sharded(
        upserts, removes, observer, n_shards, per_shard
    )


async def _batch_upsert_feeder(
    upserts: list[tuple[str, int, int]],
    removes: list[str],
    observer: str,
) -> tuple[int, int]:
    """Feed the batch via the official `vespa-feed-client` JVM subprocess.

    Streams JSONL partial-update ops to the client's stdin and reads its
    `--benchmark` summary for the op counts. Raises on spawn/parse failure so
    the caller can fall back to httpx.
    """
    total = len(upserts) + len(removes)
    # Heap: 2g fits the in-flight peak at prod scale (256m OOM'd when Vespa was
    # slow and ops backed up) while staying well under the 8Gi pod limit shared
    # with uvicorn. The JVM default (25% of the limit) would do too, but pin it.
    cmd = [
        "java", "-Xmx2g", "-jar", _FEEDER_JAR,
        "--file", "/dev/stdin",
        "--endpoint", settings.vespa_url,
        "--benchmark",
    ]
    if _FEEDER_CONNECTIONS:
        cmd += ["--connections", _FEEDER_CONNECTIONS]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        # Capture stderr (drained concurrently below) so a feeder failure — OOM,
        # bad endpoint — is logged, not swallowed into an empty-stdout parse error.
        stderr=asyncio.subprocess.PIPE,
    )

    async def _feed_stdin() -> None:
        buf: list[str] = []
        try:
            for pk, sc, fc in upserts:
                buf.append(vespa_feed.upsert_feed_line(observer, pk, sc, fc))
                if len(buf) >= 5000:
                    proc.stdin.write(("\n".join(buf) + "\n").encode())
                    await proc.stdin.drain()
                    buf.clear()
            for pk in removes:
                buf.append(vespa_feed.remove_feed_line(observer, pk))
                if len(buf) >= 5000:
                    proc.stdin.write(("\n".join(buf) + "\n").encode())
                    await proc.stdin.drain()
                    buf.clear()
            if buf:
                proc.stdin.write(("\n".join(buf) + "\n").encode())
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # feeder died early; exit code + empty stdout handled below
        finally:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()

    writer = asyncio.create_task(_feed_stdin())
    # Read both streams concurrently: the client streams periodic progress to
    # stderr during a long feed, so an undrained stderr pipe would deadlock.
    stdout_data, stderr_data = await asyncio.gather(
        proc.stdout.read(), proc.stderr.read()
    )
    await writer
    await proc.wait()

    try:
        stats = json.loads(stdout_data.decode())
    except json.JSONDecodeError:
        tail = stderr_data.decode(errors="replace").strip()[-500:]
        raise RuntimeError(
            f"feeder produced no summary (exit={proc.returncode}); stderr tail: {tail}"
        )
    ok = int(stats["feeder.ok.count"])
    failed = int(stats.get("feeder.error.count", total - ok))
    logger.info(
        f"vespa feeder: ok={ok} failed={failed} of {total} ops "
        f"in {stats.get('feeder.seconds')}s (exit={proc.returncode})"
    )
    return ok, failed


async def _batch_upsert_inprocess(
    upserts: list[tuple[str, int, int]],
    removes: list[str],
    observer: str,
    concurrency: int,
) -> tuple[int, int]:
    """Feed the batch on this process's event loop + shared client."""
    sem = asyncio.Semaphore(concurrency)

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
        for exc in failed[:5]:
            logger.warning(f"vespa score-batch op failed: {exc!r}")
        if len(failed) > 5:
            logger.warning(f"... and {len(failed) - 5} more vespa score-batch failures")
    return len(results) - len(failed), len(failed)


async def _batch_upsert_sharded(
    upserts: list[tuple[str, int, int]],
    removes: list[str],
    observer: str,
    n_shards: int,
    per_shard: int,
) -> tuple[int, int]:
    """Split the feed evenly across `n_shards` worker processes (spawn context —
    safe to fork an async parent), each feeding its slice at `per_shard`
    concurrency with its own httpx client. Sums (ok, failed); a whole shard that
    crashes counts its slice as failed (Vespa is best-effort)."""
    up_shards = vespa_feed.shard(upserts, n_shards)
    rm_shards = vespa_feed.shard(removes, n_shards)
    sizes = [len(up_shards[i]) + len(rm_shards[i]) for i in range(n_shards)]
    args = [
        (settings.vespa_url, observer, up_shards[i], rm_shards[i],
         per_shard, _CONNECT_RETRIES, _HTTP2)
        for i in range(n_shards)
    ]
    logger.info(
        f"vespa feed: {sum(sizes)} ops across {n_shards} processes @ {per_shard} conc each"
    )
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor(
        max_workers=n_shards, mp_context=mp.get_context("spawn")
    ) as pool:
        results = await asyncio.gather(
            *(loop.run_in_executor(pool, vespa_feed.feed_shard, a) for a in args),
            return_exceptions=True,
        )
    ok = failed = 0
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            logger.warning(f"vespa feed shard {i} crashed: {r!r}")
            failed += sizes[i]
            continue
        n_ok, n_failed, errs = r
        ok += n_ok
        failed += n_failed
        for e in errs[:2]:
            logger.warning(f"vespa feed op failed: {e}")
    return ok, failed


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
        # match tier (doc.sd §11/§12): "name" (name/display_name token match) >
        # "identity" (nip05/lud16, IDF-scored — the "primal" dilution) >
        # "affiliation" (about/website) > "gram" (trigram/recall noise). The
        # default profile also multiplies text by trust (wot_mult) and IDF-scores
        # identity fields (identity_text) — both surfaced below for the inspector.
        # `mf` is empty on the npub/hex direct-fetch path (no ranking ran).
        mq = mf.get("match_quality")
        fields["_match_quality"] = mq
        fields["_identity_text"] = mf.get("identity_text")
        fields["_text_score"] = mf.get("text_score")
        fields["_wot_mult"] = mf.get("wot_mult")
        if mf:
            if mq and int(mq) > 0:
                fields["_match_tier"] = _MATCH_TIERS.get(int(mq), str(mq))
            elif mf.get("name_match"):
                fields["_match_tier"] = "name"
            elif mf.get("identity_match"):
                fields["_match_tier"] = "identity"
            elif mf.get("has_token_match"):
                # older profiles fold nip05/lud16 into has_token_match
                fields["_match_tier"] = "name"
            elif mf.get("affiliation_match"):
                fields["_match_tier"] = "affiliation"
            else:
                fields["_match_tier"] = "gram"
        out.append(fields)
    return out
