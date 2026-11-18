"""`/compromised`: public check of whether an account's key is marked
compromised (`app/services/compromised_service.py`), for the UI to flag it."""

from fastapi import APIRouter, HTTPException

from app.routers.open_ranking.common import enforce_batch_size
from app.schemas.request_body_schemas import CheckCompromisedBody
from app.schemas.request_response_schemas import (
    GetCompromisedStatusesResponse,
    GetCompromisedStatusResponse,
)
from app.schemas.schemas import CompromisedStatusesData
from app.services.compromised_service import check_compromised, parse_pubkey

MAX_COMPROMISED_CHECK = 500

router = APIRouter()


def _hex(pubkey: str, field: str) -> str:
    try:
        return parse_pubkey(pubkey)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"{field} is not a pubkey (npub or hex)."
        )


@router.get(
    path="/{pubkey}",
    summary="Whether an account is marked compromised",
    description=(
        "`compromised` false, or true with `since`, the event proving it "
        "(whole), and the owner's new key once known. A key that is a "
        "compromised account's new key carries that account's record as "
        "`replaces`."
    ),
)
async def get_compromised_status_endpoint(pubkey: str) -> GetCompromisedStatusResponse:
    (status,) = await check_compromised([_hex(pubkey, "pubkey")])
    return GetCompromisedStatusResponse(data=status)


@router.post(
    path="",
    summary="Whether each of many accounts is marked compromised",
    description="One result per pubkey, in the order sent; at most 500.",
)
async def check_compromised_endpoint(
    body: CheckCompromisedBody,
) -> GetCompromisedStatusesResponse:
    # 413 before per-item validation, as /user/trustSignals does.
    enforce_batch_size(len(body.pubkeys), MAX_COMPROMISED_CHECK)
    pubkeys = [_hex(pk, f"pubkeys[{i}]") for i, pk in enumerate(body.pubkeys)]
    results = await check_compromised(pubkeys)
    return GetCompromisedStatusesResponse(data=CompromisedStatusesData(results=results))
