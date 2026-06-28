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
from app.core.loggr import loggr
from app.core.vespa import search as vespa_search
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

# Tokens the client can embed in the search string. Inspired by tapestry's
# NIP-50 extensions but kept minimal — only ``observer:<hex>`` for now.
_OBSERVER_TOKEN_RE = re.compile(r"(?:^|\s)observer:([0-9a-fA-F]{64})(?=\s|$)")


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
                        "Hex pubkey for WoT point of view. Falls back to the "
                        "instance's default observer when omitted."
                    ),
                    "format": "observer:<hex-pubkey>",
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
def _parse_search(raw: str) -> tuple[str, str | None]:
    """Pull ``observer:<hex>`` out of the search string, return clean query.

    Unknown ``key:value`` tokens are left untouched so Vespa can still match
    them as plain text (NIP-50 says relays MAY ignore unknown extensions).
    """
    observer: str | None = None
    m = _OBSERVER_TOKEN_RE.search(raw)
    if m:
        observer = m.group(1).lower()
        raw = (raw[: m.start()] + " " + raw[m.end() :]).strip()
    return raw.strip(), observer


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
    query, observer_override = _parse_search(raw_search)
    if not query:
        # Pure ``observer:<hex>`` with no actual query text — nothing to search.
        await _send_json(ws, ["EOSE", sub_id])
        return

    requested_limit = search_filter.get("limit")
    if isinstance(requested_limit, int) and requested_limit > 0:
        hits = min(requested_limit, MAX_HITS)
    else:
        hits = DEFAULT_HITS

    observer = observer_override or default_observer_pubkey()

    try:
        results = await vespa_search(
            query_text=query,
            user_pubkey=observer,
            hits=hits,
            include_zero_score_results=False,
        )
    except Exception as exc:
        logger.exception("nip50: vespa search failed: %r", exc)
        await _send_json(ws, ["NOTICE", f"search backend error: {exc!s}"])
        await _send_json(ws, ["EOSE", sub_id])
        return

    # Preserve Vespa's rank order when emitting events.
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
        "nip50: sub=%s query=%r observer=%s hits=%d emitted=%d",
        sub_id,
        query,
        observer[:12],
        len(ranked_pubkeys),
        emitted,
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
