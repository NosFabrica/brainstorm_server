"""Process-safe Vespa score-feed worker bodies.

Depends on **stdlib + httpx only** (no settings/loggr/app imports): these are the
worker bodies for a `ProcessPoolExecutor`, so on a `spawn` platform the child
re-imports this module and we want that import cheap and side-effect-free. The
orchestration that reads settings and decides whether to parallelise lives in
`app/core/vespa.py`.

Why processes: one asyncio event loop is GIL-bound to ~1 core, but a single Vespa
content node has ample headroom (measured ~2 of 16 cores during a 105k feed).
Sharding the feed across cores lifts client throughput past the one-core wall.
The request-body builders here are the single source of truth, imported by
`vespa.py` so the in-process and sharded paths stay byte-identical.
"""
import asyncio

import httpx

NAMESPACE = "doc"
DOCTYPE = "doc"

# (pubkey, score, followers)
Upsert = tuple[str, int, int]

_RETRYABLE = (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)


def _doc_url(vespa_url: str, pubkey: str) -> str:
    return f"{vespa_url}/document/v1/{NAMESPACE}/{DOCTYPE}/docid/{pubkey}"


def upsert_body(observer: str, score: int, followers: int) -> dict:
    """One partial update writing the observer's cell into BOTH tensors (kept in
    lockstep, same shape as vespa.upsert_score)."""
    return {
        "fields": {
            "quality_scores": {
                "add": {"cells": [{"address": {"user": observer}, "value": int(score)}]}
            },
            "follower_counts": {
                "add": {"cells": [{"address": {"user": observer}, "value": float(followers)}]}
            },
        }
    }


def remove_body(observer: str) -> dict:
    return {
        "fields": {
            "quality_scores": {"remove": {"addresses": [{"user": observer}]}},
            "follower_counts": {"remove": {"addresses": [{"user": observer}]}},
        }
    }


def shard(items: list, n: int) -> list[list]:
    """Round-robin `items` into exactly `n` lists (some may be empty). Order is
    irrelevant for feeding, so round-robin keeps shard sizes within 1."""
    n = max(1, n)
    out: list[list] = [[] for _ in range(n)]
    for i, it in enumerate(items):
        out[i % n].append(it)
    return out


async def _feed(
    vespa_url: str,
    observer: str,
    upserts: list,
    removes: list,
    concurrency: int,
    retries: int,
    http2: bool,
) -> tuple[int, int, list[str]]:
    try:
        client = httpx.AsyncClient(
            http1=not http2,
            http2=http2,
            timeout=httpx.Timeout(30.0, connect=1.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
        )
    except ImportError:
        # httpx[http2]/h2 not present in this image — fall back to HTTP/1.1.
        client = httpx.AsyncClient(
            http1=True,
            timeout=httpx.Timeout(30.0, connect=1.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
        )

    sem = asyncio.Semaphore(max(1, concurrency))
    ok = 0
    errors: list[str] = []

    async def _put(url, *, params=None, json=None):
        for attempt in range(retries + 1):
            try:
                return await client.put(url, params=params, json=json)
            except _RETRYABLE:
                if attempt == retries:
                    raise
                await asyncio.sleep(0.1 * (attempt + 1))

    async def _do_upsert(pk, sc, fc):
        nonlocal ok
        async with sem:
            try:
                r = await _put(
                    _doc_url(vespa_url, pk),
                    params={"create": "true"},
                    json=upsert_body(observer, sc, fc),
                )
                if r.status_code >= 400:
                    raise RuntimeError(f"upsert {pk} -> {r.status_code}: {r.text[:200]}")
                ok += 1
            except Exception as e:
                if len(errors) < 5:
                    errors.append(repr(e))

    async def _do_remove(pk):
        nonlocal ok
        async with sem:
            try:
                r = await _put(_doc_url(vespa_url, pk), json=remove_body(observer))
                if r.status_code == 404 or r.status_code < 400:
                    ok += 1  # 404 = nothing to remove, still a success
                else:
                    raise RuntimeError(f"remove {pk} -> {r.status_code}: {r.text[:200]}")
            except Exception as e:
                if len(errors) < 5:
                    errors.append(repr(e))

    try:
        tasks = [_do_upsert(pk, sc, fc) for pk, sc, fc in upserts]
        tasks += [_do_remove(pk) for pk in removes]
        await asyncio.gather(*tasks)
    finally:
        await client.aclose()

    total = len(upserts) + len(removes)
    return ok, total - ok, errors


def feed_shard(args) -> tuple[int, int, list[str]]:
    """ProcessPool worker entry point. Feeds one shard on a fresh, process-local
    event loop + httpx client. `args` is picklable:
    (vespa_url, observer, upserts, removes, concurrency, retries, http2).
    Returns (n_ok, n_failed, sample_error_reprs)."""
    vespa_url, observer, upserts, removes, concurrency, retries, http2 = args
    return asyncio.run(
        _feed(vespa_url, observer, upserts, removes, concurrency, retries, http2)
    )
