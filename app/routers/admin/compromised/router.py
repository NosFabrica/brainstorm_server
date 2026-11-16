"""`/admin/compromised`: accounts whose key is compromised. Every Observer
asserts them at rank 0 and no GrapeRank run brings their score back
(`app/services/compromised_service.py`)."""

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app.schemas.request_body_schemas import CompromisedPubkeyBody
from app.schemas.schemas import CompromisedPubkeyItem
from app.services.compromised_service import (
    add_compromised,
    list_compromised,
    parse_pubkey,
    remove_compromised,
    zero_compromised_in_background,
)

router = APIRouter()


def _hex(pubkey: str) -> str:
    try:
        return parse_pubkey(pubkey)
    except ValueError:
        raise HTTPException(status_code=422, detail="Not a pubkey (npub or hex).")


@router.get(
    path="",
    response_model=list[CompromisedPubkeyItem],
    summary="Admin: list compromised accounts",
)
async def list_compromised_endpoint():
    return await list_compromised()


@router.put(
    path="/{pubkey}",
    response_model=CompromisedPubkeyItem,
    summary="Admin: mark an account compromised and zero its scores",
    description=(
        "Records the account (npub or hex) with the event proving the compromise, "
        "then, after answering, re-signs every Observer's Trusted Assertion about "
        "it at rank 0 and clears its stored scores and whitelist entries. Every "
        "later GrapeRank run keeps it at 0. Idempotent: calling again updates the "
        "proof and reason and zeroes again (e.g. after a run that was publishing "
        "when it was first added)."
    ),
)
async def put_compromised_endpoint(
    pubkey: str,
    body: CompromisedPubkeyBody,
    request: Request,
    background: BackgroundTasks,
):
    hex_pubkey = _hex(pubkey)
    row = await add_compromised(
        hex_pubkey,
        body.proof_event_id,
        body.reason,
        request.state.jwt_data.nostr_pubkey,
    )
    background.add_task(zero_compromised_in_background, hex_pubkey)
    return row


@router.delete(
    path="/{pubkey}",
    status_code=204,
    summary="Admin: stop treating an account as compromised",
    description=(
        "Its rank-0 Trusted Assertions stay until each Observer re-asserts the "
        "account (its next run where the score changed, or its next full sync)."
    ),
)
async def delete_compromised_endpoint(pubkey: str):
    if not await remove_compromised(_hex(pubkey)):
        raise HTTPException(
            status_code=404, detail="That account is not marked compromised."
        )
    return Response(status_code=204)
