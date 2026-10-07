"""ORE-05: POST /search/pubkeys, served by the search relay and proxied here."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.core.search_relay import SearchRelayUnavailable, search_pubkeys
from app.routers.open_ranking.schemas import (
    SearchPubkeysRequest,
    SearchPubkeysResponse,
    ore_responses,
)
from app.utils.auth.nwt import optional_nwt_signer
from app.utils.observer import default_observer_pubkey

router = APIRouter()


def _forwarded(body: bytes, signer: str | None) -> bytes:
    """Name our House Observer (the relay keeps none) and, with auth on, force
    the signer as `pov`. A body that isn't a JSON object goes as is and the
    relay rejects it."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return body
    if not isinstance(parsed, dict):
        return body
    forced = {"observer": default_observer_pubkey()}
    if signer is not None:
        forced["pov"] = signer
    return json.dumps({**parsed, **forced}).encode()


@router.post(
    "/search/pubkeys",
    summary="ORE-05: search Nostr profiles by free-form text",
    description="Forwarded to the search relay's ORE-05; its answer is returned as is.",
    responses={
        200: {"model": SearchPubkeysResponse},
        **ore_responses(
            unavailable="The search relay is busy, unreachable, or answered "
            "outside the ORE-05 shape."
        ),
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": SearchPubkeysRequest.model_json_schema()}
            },
        }
    },
)
async def search_pubkeys_proxy(
    request: Request, signer: str | None = Depends(optional_nwt_signer)
) -> Response:
    body = _forwarded(await request.body(), signer)
    try:
        answer = await search_pubkeys(body)
    except SearchRelayUnavailable as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        ) from e
    return Response(
        content=answer.body, status_code=answer.status_code, headers=answer.headers
    )
