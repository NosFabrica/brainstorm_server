"""Pydantic request / response schemas for the Open Ranking endpoints
(ORE-02..ORE-07). Field names match the spec exactly — do not rename.

Also home to the ORE-00 error / retry schemas and the shared OpenAPI
`responses` table (`ore_responses`) the endpoint decorators attach, so the
Swagger surface matches what the exception handlers (errors.py) and the
pov-availability gate (availability.py) actually emit.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---- ORE-02: /stats/pubkey ----


class StatsPubkeyRequest(BaseModel):
    pubkey: str
    algorithm: str | None = None
    pov: str | None = None


class StatsPubkeyResponse(BaseModel):
    pubkey: str
    rank: float
    follows: int | None = None
    followers: int | None = None
    mutes: int | None = None
    muters: int | None = None
    reports: int | None = None
    reporters: int | None = None
    first_seen_at: int | None = None
    ttl: int | None = None


# ---- ORE-03: /rank/pubkeys ----


class RankPubkeysRequest(BaseModel):
    pubkeys: list[str]
    algorithm: str | None = None
    pov: str | None = None
    limit: int | None = None


class RankResult(BaseModel):
    pubkey: str
    rank: float


class RankPubkeysResponse(BaseModel):
    results: list[RankResult]
    ttl: int | None = None


# ---- ORE-05: /search/pubkeys ----


class SearchPubkeysRequest(BaseModel):
    query: str
    algorithm: str | None = None
    pov: str | None = None
    limit: int | None = None


class SearchPubkeysResponse(BaseModel):
    results: list[RankResult]
    ttl: int | None = None


# ---- ORE-06 / ORE-07: /followers and /muters ----


class FollowersOrMutersRequest(BaseModel):
    pubkey: str
    algorithm: str | None = None
    pov: str | None = None
    limit: int | None = None


class FollowersOrMutersResponse(BaseModel):
    results: list[RankResult]
    total: int | None = None
    ttl: int | None = None


# ---- ORE-00 §Errors: error / retry response shapes ----


class OreErrorResponse(BaseModel):
    """Body of every ORE error response: `{"error": <reason>}`, with the same
    reason mirrored in the `X-Reason` header (ORE-00 §Errors). Rendered by the
    ORE exception handlers in errors.py — these paths never emit FastAPI's
    default `{"detail": ...}` shape.
    """

    error: str = Field(
        description=(
            "Human-readable reason for the failure; also carried in the "
            "X-Reason response header."
        ),
        examples=["Algorithm 'graperank-pov' requires a 'pov' pubkey"],
    )


class OreComputingResponse(BaseModel):
    """Body of the 202 response while personalized scores for a provisioned
    pov are still being computed (unavailable-pov contract, ORE-01 §"Point of
    View").
    """

    status: Literal["computing"] = "computing"
    retry_after: int = Field(
        description=(
            "Advisory seconds to wait before retrying the identical request; "
            "also carried in the Retry-After response header."
        ),
        examples=[60],
    )


# ---- Shared OpenAPI `responses` documentation for the data endpoints ----


_X_REASON_HEADER: dict[str, Any] = {
    "description": "Human-readable explanation of the response (ORE-00 §Errors).",
    "schema": {"type": "string"},
}

_RETRY_AFTER_HEADER: dict[str, Any] = {
    "description": "Advisory seconds to wait before retrying the identical request.",
    "schema": {"type": "integer"},
}

_WWW_AUTHENTICATE_HEADER: dict[str, Any] = {
    "description": "Authentication scheme the provider expects: `Nostr` (ORE-A).",
    "schema": {"type": "string"},
}


def _error(
    description: str, extra_headers: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "model": OreErrorResponse,
        "description": description,
        "headers": {"X-Reason": _X_REASON_HEADER, **(extra_headers or {})},
    }


def ore_responses(*, batch_cap: int | None = None) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses` for the ORE data endpoints (ORE-02..ORE-07).

    Documents the shapes the ORE exception handlers (errors.py) and the
    pov-availability gate (availability.py) actually produce — including
    replacing FastAPI's auto-generated 422 `HTTPValidationError` entry, whose
    `{"detail": [...]}` body these paths never return. `batch_cap` adds the
    413 entry for endpoints that enforce a pubkey-batch limit.
    """
    responses: dict[int | str, dict[str, Any]] = {
        202: {
            "model": OreComputingResponse,
            "description": (
                "Personalized scores for the requested pov are provisioned "
                "but still being computed. Retry the identical request after "
                "`Retry-After` seconds."
            ),
            "headers": {
                "X-Reason": _X_REASON_HEADER,
                "Retry-After": _RETRY_AFTER_HEADER,
            },
        },
        400: _error("Request body is malformed or not valid JSON."),
        401: _error(
            "Authenticated mode only: the `Authorization: Nostr <token>` "
            "header is missing or the NWT is not a validly signed kind-27519 "
            "event (ORE-A).",
            {"WWW-Authenticate": _WWW_AUTHENTICATE_HEADER},
        ),
        403: _error(
            "Authenticated mode only: the NWT is validly signed but a claim "
            "is rejected — token expired, not yet valid, or its audience "
            "does not include this provider (ORE-A).",
            {"WWW-Authenticate": _WWW_AUTHENTICATE_HEADER},
        ),
    }
    if batch_cap is not None:
        responses[413] = _error(
            f"Request exceeds the provider's limit of {batch_cap} pubkeys " f"per call."
        )
    responses[422] = _error(
        "Invalid request field, unsupported `algorithm`, missing `pov` for a "
        "personalized algorithm — or the pov cannot be served (unavailable-pov "
        "contract, ORE-01: the provider never substitutes a different point "
        "of view; `X-Reason` explains the failure and names the fallback "
        "options)."
    )
    return responses
