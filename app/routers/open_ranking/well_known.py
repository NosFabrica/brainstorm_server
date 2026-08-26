"""ORE-01 capability document endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.routers.open_ranking.capabilities import CAPABILITY_DOC

router = APIRouter()


@router.get(
    "/.well-known/open-ranking.json",
    summary="Open Ranking capability document (ORE-01)",
)
async def open_ranking_capability_document() -> JSONResponse:
    # Explicitly construct JSONResponse so we control the Content-Type and can
    # set the CORS header per ORE-00 even if global CORS middleware misses it
    # (e.g. on non-Origin requests).
    return JSONResponse(
        content=CAPABILITY_DOC,
        headers={"Access-Control-Allow-Origin": "*"},
    )
