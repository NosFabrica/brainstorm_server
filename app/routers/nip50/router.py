"""NIP-50 search relay endpoint.

Exposes a single resource at ``/relay`` that speaks just enough of the Nostr
relay protocol to satisfy NIP-50 search clients:

  * ``GET /relay`` with ``Accept: application/nostr+json`` returns a NIP-11
    relay-information document advertising ``supported_nips = [1, 11, 50]``.
  * ``WebSocket /relay`` accepts NIP-01 frames. Only ``REQ`` messages whose
    filter contains a ``search`` field are honoured; everything else gets a
    quick ``EOSE`` or a polite ``OK false`` so the connection stays well-behaved
    without us pretending to be a full relay.

Search execution reuses the existing Vespa pipeline (``app.core.vespa.search``)
and then fetches the *original* signed kind-0 events from the cluster-internal
strfry instance configured via ``settings.nip50_backing_relay_url``. That way
the events we hand back have valid ``id`` + ``sig``, so any standards-compliant
client (Amethyst, etc.) will accept them.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import websockets
from fastapi import APIRouter, Header, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.core.redis_db import get_redis_client
from app.core.vespa import (
    DEFAULT_MIN_RANK,
    RANK_PROFILE_SORT_ASC,
    RANK_PROFILE_SORT_DESC,
    RANK_PROFILE_SORT_FOLLOWERS,
)
from app.core.vespa import search as vespa_search
from app.services.brainstorm_pubkey_service import get_or_create_brainstorm_pubkey
from app.utils.observer import default_observer_pubkey

logger = loggr.get_logger(__name__)

router = APIRouter()

# Anchor the supported kinds in one place. We only index kind 0 profiles.
SUPPORTED_KINDS: tuple[int, ...] = (0,)

# Cap how many hits we ever ask Vespa for in one search, regardless of what
# the client requested. Mirrors RESULTS_LIMIT in the HTTP search router.
MAX_HITS = 400
DEFAULT_HITS = 100

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Tokens the client can embed in the search string. Mirrors tapestry's
# NIP-50 extensions, restricted to what brainstorm's Vespa schema can back
# today (one per-observer metric: ``rank`` == quality_score, 0..100).
_OBSERVER_TOKEN_RE = re.compile(r"(?:^|\s)observer:([0-9a-fA-F]{64})(?=\s|$)")
_SORT_TOKEN_RE = re.compile(
    r"(?:^|\s)sort:([a-zA-Z_]+):(asc|desc)(?=\s|$)", re.IGNORECASE
)
_FILTER_TOKEN_RE = re.compile(
    r"(?:^|\s)filter:([a-zA-Z_]+):(gte|lte|gt|lt|eq):(-?\d+(?:\.\d+)?)(?=\s|$)",
    re.IGNORECASE,
)

# Per-observer metrics we can sort / filter on. Map of NIP-50 metric name →
# the field in the dict returned by vespa.search(). Adding more metrics later
# (when the Vespa schema grows additional sparse tensors) is a matter of
# extending this mapping, the rank profiles in doc.sd, and the NIP-11
# advertisement below.
_METRIC_FIELDS: dict[str, str] = {
    "rank": "_quality_score",
    "followers": "_followers",
}

# Sort can use either metric (`followers` is the default order). Filter can
# currently only use `rank` — Vespa gates on query(min_rank) over the rank
# tensor; there's no follower-count gate yet. See docs/search-vs-tapestry.md §8.
_SORT_METRICS: frozenset = frozenset({"rank", "followers"})
_FILTER_METRICS: frozenset = frozenset({"rank"})

# Filter operators we can push into Vespa. The rank_* profiles gate on
# query(min_rank) via Vespa's rank-score-drop-limit, which is a lower-bound
# (>=) mechanism — so only gte/gt map natively. lte/lt/eq would need the score
# stored as a range-queryable attribute (a schema re-model + re-feed); they are
# intentionally unsupported and the relay NOTICEs the client. See
# docs/search-precision-and-filtering.md (Problem 2).
_FILTER_OPS: tuple[str, ...] = ("gte", "gt")

# gt:N is pushed down as min_rank = N + epsilon (a >= gate that excludes N).
# rank is an integer 0..100, so any epsilon in (0, 1) is exact.
_GT_EPSILON = 1e-6

# Cold-start dedup: once we've provisioned graperank for an observer we don't
# want to re-fire it on every search. The Redis NX key auto-expires so a
# stale observer (e.g. one whose scores never landed because graperank
# errored) eventually gets re-attempted.
PROVISION_DEDUP_KEY_FMT = "nip50:provisioned:{pubkey}"
PROVISION_DEDUP_TTL_SECONDS = 3600


# ---------------------------------------------------------------------------
# NIP-11 relay info
# ---------------------------------------------------------------------------
def _nip11_document() -> dict[str, Any]:
    return {
        "name": "brainstorm-search",
        "description": (
            "NIP-50 search-only relay backed by the Brainstorm Vespa profile "
            "index. Returns kind-0 profile events ranked by Web-of-Trust "
            "quality score from the observer's perspective."
        ),
        "supported_nips": [1, 11, 50],
        "software": "brainstorm_server",
        "version": "0.1.0",
        "limitation": {
            "auth_required": False,
            "payment_required": False,
            "restricted_writes": True,
        },
        "search_capabilities": {
            "supported_kinds": list(SUPPORTED_KINDS),
            "extensions": {
                "observer": {
                    "description": (
                        "Hex pubkey for WoT point of view. The first time a "
                        "new observer is used the relay enqueues a GrapeRank "
                        "computation in the background; meanwhile the search "
                        "falls back to the instance's default observer."
                    ),
                    "format": "observer:<hex-pubkey>",
                },
                "sort": {
                    "description": (
                        "Sort hits by a WoT metric. When omitted, the relay's "
                        "default rank profile (text relevance \u00d7 quality "
                        "boost from the observer's perspective) is used."
                    ),
                    "format": "sort:<metric>:<asc|desc>",
                    "metrics": sorted(_METRIC_FIELDS.keys()),
                },
                "filter": {
                    "description": (
                        "Drop hits whose metric value fails the given "
                        "comparison. Only lower-bound operators are supported "
                        "(the relay pushes them into Vespa as a minimum-rank "
                        "cut-off); other operators are ignored with a NOTICE. "
                        "Multiple filter tokens are AND-ed (the most "
                        "restrictive lower bound wins)."
                    ),
                    "format": "filter:<metric>:<op>:<value>",
                    "metrics": sorted(_METRIC_FIELDS.keys()),
                    "operators": sorted(_FILTER_OPS),
                    "scales": {
                        "rank": (
                            "0-100 integer; the observer's GrapeRank "
                            "influence score (round(influence * 100))."
                        ),
                    },
                },
            },
        },
    }


@router.get(
    path="/relay",
    summary="NIP-11 relay information / landing page",
    include_in_schema=False,
)
async def relay_info(
    request: Request,
    accept: str | None = Header(default=None),
) -> Response:
    """Serve the NIP-11 document when negotiated, else a plain landing page.

    Per NIP-11, a relay advertises its capabilities at the same URL clients
    connect to over WebSocket, distinguished only by the ``Accept`` header.
    """
    if accept and "application/nostr+json" in accept.lower():
        body = json.dumps(_nip11_document())
        return Response(
            content=body,
            media_type="application/nostr+json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    return Response(
        content=(
            "Brainstorm NIP-50 search relay. Connect via WebSocket to this "
            "same URL and send a NIP-01 REQ with a 'search' filter."
        ),
        media_type="text/plain",
    )


# ---------------------------------------------------------------------------
# Search string parsing
# ---------------------------------------------------------------------------
def _parse_search(
    raw: str,
) -> tuple[
    str,
    str | None,
    tuple[str, str] | None,
    list[tuple[str, str, float]],
    list[str],
]:
    """Strip NIP-50 extensions out of the search string.

    Returns ``(clean_query, observer, sort_spec, filters, notices)`` where
    ``sort_spec`` is ``(metric, direction)`` or ``None``, ``filters`` is a
    list of ``(metric, op, value)`` triples (AND-ed at apply time), and
    ``notices`` collects human-readable strings describing tokens we parsed
    but had to drop (unknown metric, unsupported op, etc.) so the caller can
    relay them to the client via NIP-01 ``NOTICE`` frames.

    Unknown ``key:value`` tokens that don't match any of our extension
    regexes are left in the query — NIP-50 allows relays to treat unknown
    extensions as plain text (and Vespa's text matcher will simply ignore
    them if they don't match any indexed term).
    """
    observer: str | None = None
    sort_spec: tuple[str, str] | None = None
    filters: list[tuple[str, str, float]] = []
    notices: list[str] = []

    def _strip(text: str, start: int, end: int) -> str:
        return (text[:start] + " " + text[end:]).strip()

    m = _OBSERVER_TOKEN_RE.search(raw)
    if m:
        observer = m.group(1).lower()
        raw = _strip(raw, m.start(), m.end())

    # Only the LAST sort token wins if multiple are supplied; same as how
    # most query languages handle conflicting order-by clauses.
    for m in list(_SORT_TOKEN_RE.finditer(raw)):
        metric = m.group(1).lower()
        direction = m.group(2).lower()
        if metric not in _SORT_METRICS:
            notices.append(
                f"sort metric {metric!r} not supported; ignoring (supported: "
                f"{', '.join(sorted(_SORT_METRICS))})"
            )
        else:
            sort_spec = (metric, direction)
    raw = _SORT_TOKEN_RE.sub(" ", raw).strip()

    for m in list(_FILTER_TOKEN_RE.finditer(raw)):
        metric = m.group(1).lower()
        op = m.group(2).lower()
        try:
            value = float(m.group(3))
        except ValueError:
            notices.append(f"filter value {m.group(3)!r} not numeric; ignoring")
            continue
        if metric not in _FILTER_METRICS:
            notices.append(
                f"filter metric {metric!r} not supported; ignoring "
                f"(supported: {', '.join(sorted(_FILTER_METRICS))})"
            )
            continue
        if op not in _FILTER_OPS:
            notices.append(
                f"filter op {op!r} not supported; ignoring "
                f"(supported: {', '.join(sorted(_FILTER_OPS))})"
            )
            continue
        filters.append((metric, op, value))
    raw = _FILTER_TOKEN_RE.sub(" ", raw).strip()

    return raw, observer, sort_spec, filters, notices


def _select_ranking(
    sort_spec: tuple[str, str] | None,
    filters: list[tuple[str, str, float]],
) -> tuple[str | None, float | None]:
    """Map parsed sort/filter tokens to a Vespa rank profile + min_rank.

    Returns ``(ranking_profile, min_rank)`` to hand to ``vespa.search``:

      * ``ranking_profile`` is always one of the ``RANK_PROFILE_*`` names. The
        NIP-50 default (no sort token) is ``sort_followers`` — text match, sorted
        by verified-follower count (docs/search-vs-tapestry.md §8.1).
        ``sort:followers`` keeps that; ``sort:rank:desc/asc`` orders by trust.
      * ``min_rank`` is the rank>=N filter floor: the most restrictive
        ``filter:rank`` lower bound if any (``gte`` → value, ``gt`` → value +
        epsilon), otherwise the product default ``DEFAULT_MIN_RANK`` (rank>=2).
        Vespa drops hits scoring below it.

    All sort/filter happens inside Vespa — there is no Python post-pass. Sort
    supports ``rank`` and ``followers``; filter supports ``rank`` only.
    """
    min_rank: float | None = None
    for _metric, op, value in filters:
        threshold = value if op == "gte" else value + _GT_EPSILON
        min_rank = threshold if min_rank is None else max(min_rank, threshold)
    # No explicit filter → apply the default rank>=2 floor (§8.1). An explicit
    # filter:rank overrides it (even lower), since that's the caller configuring.
    if min_rank is None:
        min_rank = DEFAULT_MIN_RANK

    if sort_spec is not None:
        metric, direction = sort_spec
        if metric == "followers":
            profile = RANK_PROFILE_SORT_FOLLOWERS  # followers is desc-only
        elif direction == "asc":
            profile = RANK_PROFILE_SORT_ASC
        else:
            profile = RANK_PROFILE_SORT_DESC
    else:
        # No sort: the popular-first default.
        profile = RANK_PROFILE_SORT_FOLLOWERS

    return profile, min_rank


# ---------------------------------------------------------------------------
# Strfry round-trip for original signed events
# ---------------------------------------------------------------------------
async def _fetch_kind0_events(authors: list[str]) -> dict[str, dict]:
    """Fetch latest kind-0 events for the given authors from internal strfry.

    Returns a mapping ``pubkey -> event``. Missing authors are simply absent.
    On timeout or transport error we log and return whatever we have so far —
    the search result degrades gracefully rather than failing the whole REQ.
    """
    if not authors:
        return {}

    sub_id = f"nip50-{id(authors)}"
    req = json.dumps(
        ["REQ", sub_id, {"kinds": [0], "authors": authors, "limit": len(authors)}]
    )
    close_msg = json.dumps(["CLOSE", sub_id])
    events: dict[str, dict] = {}

    url = settings.nip50_backing_relay_url
    timeout = settings.nip50_strfry_timeout_seconds

    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(url, open_timeout=timeout) as ws:
                await ws.send(req)
                while True:
                    raw = await ws.recv()
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(msg, list) or not msg:
                        continue
                    if msg[0] == "EVENT" and len(msg) >= 3 and msg[1] == sub_id:
                        ev = msg[2]
                        pk = ev.get("pubkey")
                        # Keep the newest event per author (strfry usually
                        # returns newest-first, but be defensive).
                        prev = events.get(pk)
                        if prev is None or ev.get("created_at", 0) >= prev.get(
                            "created_at", 0
                        ):
                            events[pk] = ev
                    elif msg[0] == "EOSE" and len(msg) >= 2 and msg[1] == sub_id:
                        try:
                            await ws.send(close_msg)
                        except Exception:
                            pass
                        break
                    elif msg[0] == "CLOSED" and len(msg) >= 2 and msg[1] == sub_id:
                        break
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "nip50: strfry fetch timed out after %.1fs (got %d/%d events)",
            timeout,
            len(events),
            len(authors),
        )
    except Exception as exc:
        logger.warning("nip50: strfry fetch failed: %r", exc)

    return events


# ---------------------------------------------------------------------------
# WebSocket message handling
# ---------------------------------------------------------------------------
async def _send_json(ws: WebSocket, payload: list) -> None:
    """Send a NIP-01 frame; swallow disconnects so handlers can keep looping."""
    try:
        await ws.send_text(json.dumps(payload))
    except (WebSocketDisconnect, RuntimeError):
        # Caller will notice on the next recv() and exit the loop.
        pass


def _filter_supports_kind0(f: dict) -> bool:
    """A NIP-01 filter matches kind 0 iff ``kinds`` is absent or contains 0."""
    kinds = f.get("kinds")
    if kinds is None:
        return True
    return isinstance(kinds, list) and 0 in kinds


async def _handle_req(ws: WebSocket, msg: list) -> None:
    if len(msg) < 3 or not isinstance(msg[1], str):
        return
    sub_id: str = msg[1]
    filters = [f for f in msg[2:] if isinstance(f, dict)]

    # Find the first filter that (a) carries a search string and (b) could
    # plausibly match kind 0. Anything else gets an empty EOSE — we don't
    # serve general Nostr traffic from this endpoint.
    search_filter = next(
        (
            f
            for f in filters
            if isinstance(f.get("search"), str)
            and f["search"].strip()
            and _filter_supports_kind0(f)
        ),
        None,
    )
    if search_filter is None:
        await _send_json(ws, ["EOSE", sub_id])
        return

    raw_search: str = search_filter["search"]
    query, observer_override, sort_spec, filters, parse_notices = _parse_search(
        raw_search
    )
    for note in parse_notices:
        await _send_json(ws, ["NOTICE", note])

    if not query:
        # Pure extension tokens with no actual query text — nothing to search.
        await _send_json(ws, ["EOSE", sub_id])
        return

    requested_limit = search_filter.get("limit")
    if isinstance(requested_limit, int) and requested_limit > 0:
        hits = min(requested_limit, MAX_HITS)
    else:
        hits = DEFAULT_HITS

    # Sort + filter are pushed into Vespa via a dedicated rank profile and a
    # query(min_rank) cut-off, so Vespa's top-N IS the answer — no over-fetch,
    # no Python re-ranking. See docs/search-precision-and-filtering.md.
    ranking_profile, min_rank = _select_ranking(sort_spec, filters)

    # Vespa already drops hits below query(min_rank) (default rank>=2, §8.1), so
    # the Python zero-score post-filter is redundant here — leave it off.
    include_unscored = False

    observer = observer_override or default_observer_pubkey()

    # Fire cold-start provisioning before running the search — it's a
    # fire-and-forget background coroutine so it never delays the response.
    # Only triggered for explicit observers (the default observer is always
    # provisioned by the periodic graperank cronjob).
    if observer_override:
        asyncio.create_task(_maybe_provision_observer(observer_override))

    try:
        results = await vespa_search(
            query_text=query,
            user_pubkey=observer,
            hits=hits,
            include_zero_score_results=include_unscored,
            ranking_profile=ranking_profile,
            min_rank=min_rank,
        )
    except Exception as exc:
        logger.exception("nip50: vespa search failed: %r", exc)
        await _send_json(ws, ["NOTICE", f"search backend error: {exc!s}"])
        await _send_json(ws, ["EOSE", sub_id])
        return

    # Results already arrive in Vespa rank order, filtered + sorted as asked.
    ranked_pubkeys: list[str] = []
    seen: set[str] = set()
    for hit in results:
        pk = hit.get("pubkey")
        if isinstance(pk, str) and HEX64_RE.match(pk) and pk not in seen:
            ranked_pubkeys.append(pk)
            seen.add(pk)

    events_by_pubkey = await _fetch_kind0_events(ranked_pubkeys)

    emitted = 0
    for pk in ranked_pubkeys:
        ev = events_by_pubkey.get(pk)
        if ev is None:
            continue
        await _send_json(ws, ["EVENT", sub_id, ev])
        emitted += 1

    await _send_json(ws, ["EOSE", sub_id])
    logger.info(
        "nip50: sub=%s query=%r observer=%s profile=%s min_rank=%s "
        "returned=%d emitted=%d",
        sub_id,
        query,
        observer[:12],
        ranking_profile or "default",
        min_rank,
        len(results),
        emitted,
    )


# ---------------------------------------------------------------------------
# Cold-start: trigger graperank for a fresh observer on first use
# ---------------------------------------------------------------------------
async def _maybe_provision_observer(observer: str) -> None:
    """Fire-and-forget GrapeRank provisioning for a new observer.

    First search for an observer the relay hasn't seen before triggers a
    GrapeRank job for that observer; the search itself returns whatever
    Vespa has now (typically a mix of the default observer's scores plus
    zero-score hits) and subsequent searches will see the personalized
    scores once the job completes.

    Dedup is via a Redis NX key so a burst of searches for the same
    just-arrived observer fires exactly one GrapeRank job. The key expires
    after an hour so a job that errored gets re-attempted.

    This function never raises — every failure mode is logged-and-swallowed
    because the search itself has already returned (or is about to). The
    user-visible behavior is at worst \"first few searches not personalized
    yet\" rather than a search error.
    """
    redis_client = get_redis_client()
    key = PROVISION_DEDUP_KEY_FMT.format(pubkey=observer)
    try:
        # SET key val NX EX ttl — returns truthy only the first time.
        first = await redis_client.set(
            key, "1", nx=True, ex=PROVISION_DEDUP_TTL_SECONDS
        )
    except Exception as exc:
        # Redis unhealthy — log and skip rather than spamming GrapeRank.
        logger.warning("nip50: redis dedup check failed for %s: %r", observer[:12], exc)
        return
    if not first:
        return
    try:
        async with db_session() as db:
            result = await get_or_create_brainstorm_pubkey(
                db, nostr_pubkey=observer
            )
            await db.commit()
        logger.info(
            "nip50: cold-start provisioning for observer=%s triggered=%s",
            observer[:12],
            bool(result.triggered_graperank),
        )
    except Exception as exc:
        # Drop the dedup key so the next search can retry.
        try:
            await redis_client.delete(key)
        except Exception:
            pass
        logger.warning(
            "nip50: cold-start provisioning failed for %s: %r",
            observer[:12],
            exc,
        )


@router.websocket("/relay")
async def relay_ws(ws: WebSocket) -> None:
    """Single WebSocket endpoint that speaks NIP-01 framing for NIP-50 only."""
    await ws.accept(subprotocol=None)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                await _send_json(ws, ["NOTICE", "invalid json"])
                continue
            if not isinstance(msg, list) or not msg:
                await _send_json(ws, ["NOTICE", "invalid frame"])
                continue

            verb = msg[0]
            if verb == "REQ":
                await _handle_req(ws, msg)
            elif verb == "CLOSE":
                # No per-sub state to clean up — searches complete synchronously.
                continue
            elif verb == "EVENT":
                ev = msg[1] if len(msg) >= 2 and isinstance(msg[1], dict) else {}
                ev_id = ev.get("id", "")
                await _send_json(
                    ws,
                    ["OK", ev_id, False, "blocked: read-only NIP-50 search relay"],
                )
            elif verb in ("AUTH", "COUNT"):
                # We don't implement these; stay silent (per spec, AUTH is
                # optional; COUNT requires NIP-45 which we don't advertise).
                continue
            else:
                await _send_json(ws, ["NOTICE", f"unsupported verb: {verb}"])
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.warning("nip50: ws handler crashed: %r", exc)
        try:
            await ws.close()
        except Exception:
            pass
