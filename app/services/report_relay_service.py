"""Read an author's surviving kind-1984 reports back from our own neofry.

Failures return `None` ("unknown"), never `[]` ("no reports") — the caller
reconciles by deletion, so collapsing the two would wipe live edges on a hiccup.
"""
from __future__ import annotations

import asyncio
import json

import websockets

from app.core.config import settings
from app.core.loggr import loggr

logger = loggr.get_logger(__name__)

_REPORT_FETCH_LIMIT = 10_000

# strfry clamps a filter to `maxFilterLimit` silently (filters.h), so a response
# landing exactly on a known cap (500 = stock conf, 100000 = k8s charts) may be
# truncated. Not discoverable per-REQ, so treat those counts as untrustworthy.
_LIMIT_SENTINELS = frozenset({500, 100_000, _REPORT_FETCH_LIMIT})

# Internal relay call; not worth an env knob. Generous vs the NIP-50 default (3s)
# because this filter can return an author's whole report set.
_FETCH_TIMEOUT_SECONDS = 10.0


async def fetch_author_user_reports(author: str) -> list[dict] | None:
    """Every kind-1984 the relay still holds for `author`.

    Returns `None` — meaning *unknown*, reconcile nothing — on transport error,
    timeout, or a response we suspect the relay truncated. Returns a (possibly
    empty) list only when the set is known to be complete.
    """
    sub_id = f"report-recompute-{author[:8]}"
    req = json.dumps(
        [
            "REQ",
            sub_id,
            {"kinds": [1984], "authors": [author], "limit": _REPORT_FETCH_LIMIT},
        ]
    )
    close_msg = json.dumps(["CLOSE", sub_id])
    events: list[dict] = []

    # The relay holding our data: the same neofry the transferer writes into and
    # whose writes fire the `strfry:events` rpush we're reacting to. Set in every
    # env already, so this needs no new config.
    url = settings.nostr_transfer_to_relay
    timeout = _FETCH_TIMEOUT_SECONDS

    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(
                url, open_timeout=timeout, max_size=None
            ) as ws:
                await ws.send(req)
                while True:
                    raw = await ws.recv()
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(msg, list) or len(msg) < 2 or msg[1] != sub_id:
                        continue
                    if msg[0] == "EVENT" and len(msg) >= 3:
                        events.append(msg[2])
                    elif msg[0] == "EOSE":
                        try:
                            await ws.send(close_msg)
                        except Exception:
                            pass
                        break
                    elif msg[0] == "CLOSED":
                        # The relay ended the sub itself; we have a partial set.
                        logger.warning(
                            "report recompute: relay CLOSED the sub for %s: %s",
                            author,
                            msg[2] if len(msg) >= 3 else "",
                        )
                        return None
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "report recompute: relay fetch for %s timed out after %.1fs",
            author,
            timeout,
        )
        return None
    except Exception as exc:
        logger.warning("report recompute: relay fetch for %s failed: %r", author, exc)
        return None

    if len(events) in _LIMIT_SENTINELS:
        logger.warning(
            "report recompute: fetch for %s returned %d events, matching a known "
            "relay filter cap — refusing to reconcile against a possibly truncated "
            "set. Run scripts/backfill_user_reports.py to repair this author.",
            author,
            len(events),
        )
        return None

    return events
