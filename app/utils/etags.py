"""Validators for conditional GET.

RFC 7232 reserves "weak" for a `W/`-prefixed validator compared by semantic
equivalence. These are plain quoted digests — strong validators — so nothing
here is named weak.
"""

import hashlib

from fastapi import Response

# One caller's own rows: not a shared cache's business.
PRIVATE_CACHE_CONTROL = "private, no-cache"


def etag_digest(*parts: object) -> str:
    """An ETag over everything that decides what the response would contain.

    Every part that varies the body must be passed, or a client holding the
    tag for one view is told a different view is unchanged.
    """
    joined = ":".join("" if p is None else str(p) for p in parts)
    return f'"{hashlib.sha1(joined.encode()).hexdigest()}"'


def not_modified(etag: str) -> Response:
    """The bodyless answer to a poll that has nothing to collect."""
    return Response(
        status_code=304,
        headers={"ETag": etag, "Cache-Control": PRIVATE_CACHE_CONTROL},
    )


def tag_response(response: Response, etag: str) -> None:
    """Mark a full response so the next poll can be answered with `not_modified`."""
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = PRIVATE_CACHE_CONTROL
