"""App-level exception handlers for the Open Ranking surface.

ORE-00 §Errors: providers SHOULD explain every error in the `X-Reason`
response header. FastAPI's default handlers emit `{"detail": ...}` with no
header, so these handlers reshape errors *on ORE paths only* into the
ecosystem shape `{"error": <reason>}` + `X-Reason` (the body shape the R&D
tapestry provider already uses), and route `PovComputing` to `202` +
`Retry-After`. Every other route keeps FastAPI's default behavior.

Registered on the real app by `app.api` and mirrored by the Open Ranking test
app factory (tests/open_ranking/conftest.py) so tests exercise the deployed
error shape.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.routers.open_ranking.availability import PovComputing
from app.routers.open_ranking.capabilities import CAPABILITY_DOC
from app.routers.open_ranking.common import ore_error_response


WELL_KNOWN_PATH = "/.well-known/open-ranking.json"

# The exact protocol paths — root-mounted, so path equality is the correct
# discriminator (no prefixes to worry about).
ORE_PATHS: frozenset[str] = frozenset(CAPABILITY_DOC) | {WELL_KNOWN_PATH}


def _is_ore_request(request: Request) -> bool:
    return request.url.path in ORE_PATHS


def _header_safe(reason: str) -> str:
    """HTTP header values are latin-1; anything else would 500 mid-response.
    The JSON body keeps the exact reason — only the header copy is coerced."""
    return reason.encode("latin-1", errors="replace").decode("latin-1")


async def _ore_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    if not _is_ore_request(request):
        return await http_exception_handler(request, exc)
    headers = dict(exc.headers or {})
    # Raisers that already attach their own X-Reason (e.g. the NWT auth
    # dependency) win; everything else derives it from the detail string.
    reason = headers.pop("X-Reason", None) or (
        exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    )
    return ore_error_response(exc.status_code, reason, headers=headers)


async def _ore_validation_exception(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    if not _is_ore_request(request):
        return await request_validation_exception_handler(request, exc)
    errors = exc.errors()
    if any(e.get("type") == "json_invalid" for e in errors):
        # ORE-02..07 error tables: 400 = "the request body is malformed or
        # not valid JSON" (FastAPI's default would be 422).
        status_code, reason = 400, "request body is malformed or not valid JSON"
    else:
        status_code = 422
        parts = []
        for e in errors:
            loc = ".".join(str(p) for p in e.get("loc", ()) if p != "body")
            msg = e.get("msg", "invalid value")
            parts.append(f"{loc}: {msg}" if loc else msg)
        reason = "; ".join(parts) or "invalid request body"
    return ore_error_response(status_code, reason)


async def _ore_pov_computing(request: Request, exc: PovComputing) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={"status": "computing", "retry_after": exc.retry_after},
        headers={
            "X-Reason": _header_safe(exc.reason),
            "Retry-After": str(exc.retry_after),
        },
    )


def install_ore_error_handlers(app: FastAPI) -> None:
    """Register the ORE error shape on an app. Handlers self-scope to ORE
    paths and delegate everything else to FastAPI's defaults, so installing
    them app-wide is safe."""
    app.add_exception_handler(StarletteHTTPException, _ore_http_exception)
    app.add_exception_handler(RequestValidationError, _ore_validation_exception)
    app.add_exception_handler(PovComputing, _ore_pov_computing)
