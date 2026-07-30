"""NIP-05 verification endpoint: GET /.well-known/nostr.json."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.services.nip05_service import build_nip05_document

router = APIRouter()


@router.get(
    "/.well-known/nostr.json",
    summary="NIP-05 verification for Brainstorm Assistants",
)
async def nip05_well_known(
    name: str | None = Query(default=None),
    db: AsyncDBSession = Depends(get_db),
) -> JSONResponse:
    # No name, no lookup — never dump the list; that's an enumeration surface.
    document = await build_nip05_document(db, name) if name else {"names": {}}

    # Explicit JSONResponse so the CORS header is set even without an Origin,
    # which the middleware would skip (mirrors open_ranking/well_known.py).
    return JSONResponse(
        content=document,
        headers={"Access-Control-Allow-Origin": "*"},
    )
