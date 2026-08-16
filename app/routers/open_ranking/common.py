"""Shared helpers for the Open Ranking endpoints: pubkey validation, error
responses with the ORE `X-Reason` header, and TTL hints.
"""

from __future__ import annotations

import re

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Per-endpoint TTL hints (seconds). The protocol treats these as advisory.
# Aligned with how frequently the underlying data changes:
#   stats / rank / followers / muters -> GrapeRank cadence (hour-ish)
#   search -> Vespa index updates near-real-time but profile churn matters less
TTL_HINTS: dict[str, int] = {
    "/stats/pubkey": 3600,
    "/rank/pubkeys": 3600,
    "/followers": 3600,
    "/muters": 3600,
    "/search/pubkeys": 300,
}


# Hard limits on batch / list sizes the spec recommends clients respect.
MAX_BATCH_PUBKEYS = 1000
MAX_LIMIT = 1000
DEFAULT_RECOMMEND_LIMIT = 100
MAX_QUERY_LEN = 512


def validate_pubkey(value: str, field_name: str) -> str:
    """Validate ORE-00 pubkey format (64-char lowercase hex) and return the
    normalised value. Raises 422 on failure (per ORE-02/03 error tables).
    """
    if not isinstance(value, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must be a 64-character lowercase hex string",
        )
    normalised = value.lower()
    if not HEX64_RE.match(normalised):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} is not a valid 64-character lowercase hex pubkey",
        )
    return normalised


def validate_pubkey_list(values: list[str], field_name: str) -> list[str]:
    """Validate every entry in a list of pubkeys. Returns the normalised list."""
    if not isinstance(values, list) or len(values) == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must be a non-empty array",
        )
    return [validate_pubkey(p, f"{field_name}[{i}]") for i, p in enumerate(values)]


def enforce_batch_size(n: int) -> None:
    """Enforce the ORE-03 / ORE-08 1000-pubkey soft cap with HTTP 413."""
    if n > MAX_BATCH_PUBKEYS:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"Request exceeds the provider's limit of "
                f"{MAX_BATCH_PUBKEYS} pubkeys per call"
            ),
        )


def validate_positive_limit(
    limit: int | None,
    *,
    field_name: str = "limit",
    max_value: int = MAX_LIMIT,
) -> int | None:
    """Validate that an optional `limit` is a positive int within bounds."""
    if limit is None:
        return None
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must be a positive integer",
        )
    if limit > max_value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} exceeds the provider's maximum of {max_value}",
        )
    return limit


def ore_error_response(
    status_code: int, reason: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    """Build a JSON error response with the ORE-00 `X-Reason` header set.

    `headers` carries through any additional response headers (e.g. the auth
    dependency's WWW-Authenticate). HTTP header values are latin-1, so the
    header copy of the reason is coerced; the JSON body keeps it exact.
    """
    merged = dict(headers or {})
    merged["X-Reason"] = reason.encode("latin-1", errors="replace").decode("latin-1")
    return JSONResponse(
        status_code=status_code,
        content={"error": reason},
        headers=merged,
    )
