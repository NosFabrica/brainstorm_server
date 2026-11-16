"""`/admin/compromised`: accounts whose key is compromised. Every Observer
asserts them at rank 0 and no GrapeRank run brings their score back
(`app/services/compromised_service.py`)."""

from fastapi import APIRouter, HTTPException, Request, Response

from app.schemas.request_body_schemas import (
    CompromisedMetadataBody,
    CompromisedPubkeyBody,
)
from app.schemas.schemas import CompromisedPubkeyItem
from app.services.compromised_service import (
    add_compromised,
    get_compromised,
    list_compromised,
    parse_pubkey,
    remove_compromised,
    update_compromised_metadata,
    zero_now,
)

# How long PUT waits for the zeroing before answering. A run usually takes a few
# seconds; one that outlasts this keeps going and records its outcome, which
# GET shows. Kept under the UI's request timeout, with room for the proof fetch.
ZERO_WAIT_SECONDS = 20.0

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
        "kept whole (`proof_event`, or `proof_event_id` for the server to fetch), "
        "and optionally the owner's new key (`successor_pubkey`). Fields left out "
        "keep their stored values. Then zeroes its scores at once: every "
        "Observer holding a Trusted Assertion about it re-signs it at rank 0, and "
        "its stored scores, whitelist entries and own ratings are cleared. Answers "
        "when that is done, with its outcome in `last_zero_report`; a zeroing that "
        "takes longer than 20 s finishes after the answer (`last_zero_report` "
        "still the previous one) and GET shows it. A zeroing that never reports or "
        "doesn't succeed is retried by the server. Every later GrapeRank run keeps "
        "the account at 0. Idempotent: calling again updates what it sends and "
        "zeroes again."
    ),
)
async def put_compromised_endpoint(
    pubkey: str,
    body: CompromisedPubkeyBody,
    request: Request,
):
    hex_pubkey = _hex(pubkey)
    row = await add_compromised(hex_pubkey, body, request.state.jwt_data.nostr_pubkey)
    await zero_now(hex_pubkey, wait_seconds=ZERO_WAIT_SECONDS)
    return await get_compromised(hex_pubkey) or row


@router.patch(
    path="/{pubkey}",
    response_model=CompromisedPubkeyItem,
    summary="Admin: change a compromised account's successor or note",
    description=(
        "Sets `successor_pubkey` (npub or hex) and/or `reason`; a field left out "
        "stays, `null` clears it. The proof stays as stored and nothing is zeroed "
        "again. 404 when the account isn't marked."
    ),
)
async def patch_compromised_endpoint(pubkey: str, body: CompromisedMetadataBody):
    return await update_compromised_metadata(_hex(pubkey), body)


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
