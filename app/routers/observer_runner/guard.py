"""Who reaches `/internal/observerRunner/*`, decided before any routing.

Two pure-ASGI middlewares:

- `RequireRunnerToken` wraps the internal app (`app/internal_api.py`, its own
  port). Without `Authorization: Bearer <OBSERVER_RUNNER_TOKEN>` every request,
  whatever its method or path, gets the same 404 a missing path gets: no 405 +
  `Allow` for a wrong method, no 307 for a trailing slash, no 422 for a body.
- `RefuseInternalPaths` wraps the public app: `/internal` and anything under it
  is a 404 there, token or not, before routing. The internal router isn't
  mounted on the public app at all; this keeps it so if someone mounts it.

The token is stripped (a Secret mounted with a trailing newline still works)
and must be at least `MIN_TOKEN_LENGTH` characters: a shorter one leaves the
internal API off (`token_refusal` says why, logged at startup).
"""
from __future__ import annotations

import hmac

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings

INTERNAL_PREFIX = "/internal"
MIN_TOKEN_LENGTH = 32

# The body FastAPI answers for a path that doesn't exist.
_NOT_FOUND = {"detail": "Not Found"}


def token_refusal() -> str | None:
    """Why the configured token can't enable the internal API, or None."""
    token = settings.observer_runner_token.strip()
    if not token:
        return "OBSERVER_RUNNER_TOKEN is not set"
    if len(token) < MIN_TOKEN_LENGTH:
        return f"OBSERVER_RUNNER_TOKEN is shorter than {MIN_TOKEN_LENGTH} characters"
    return None


def bearer_matches(authorization: str) -> bool:
    """Is `authorization` `Bearer <the configured token>`? Compared in constant
    time, both sides stripped. False whenever the token can't be used."""
    if token_refusal() is not None:
        return False
    expected = settings.observer_runner_token.strip()
    scheme, _, given = authorization.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(
        given.strip().encode(), expected.encode()
    )


def is_internal_path(path: str) -> bool:
    """`/internal` or below it, however the slashes or the case are written."""
    normalized = "/" + path.lstrip("/").lower()
    return normalized == INTERNAL_PREFIX or normalized.startswith(INTERNAL_PREFIX + "/")


async def _refuse(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    await JSONResponse(_NOT_FOUND, status_code=404)(scope, receive, send)


class RequireRunnerToken:
    """The internal app: no worker token, no route, for every method and path."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not bearer_matches(
            Headers(scope=scope).get("authorization", "")
        ):
            await _refuse(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RefuseInternalPaths:
    """The public app: `/internal/` doesn't exist here."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and is_internal_path(
            scope.get("path", "")
        ):
            await _refuse(scope, receive, send)
            return
        await self.app(scope, receive, send)
