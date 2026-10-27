from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.database import get_db
from app.schemas.request_body_schemas import OpenPasskeyVaultBody, PutPasskeyVaultBody
from app.schemas.request_response_schemas import (
    PasskeyVaultBlobResponse,
    PasskeyVaultChallengeResponse,
    PasskeyVaultDeletedResponse,
    PasskeyVaultEntryResponse,
    PasskeyVaultListResponse,
)
from app.schemas.schemas import PasskeyVaultBlob, PasskeyVaultChallenge
from app.services.passkey_vault_service import (
    delete_vault,
    issue_challenge,
    list_vaults,
    open_vault,
    store_vault,
)
from app.utils.rate_limiting.rate_limiting import rate_limit_per_ip

# The owner's half: writing, listing and removing their own rows. Session-gated.
router = APIRouter()
# The new device's half: it has no Session yet, so the passkey is the credential.
public_router = APIRouter()

CREDENTIAL_ID_HASH = Path(pattern=r"^[0-9a-f]{64}$")

rate_limit_passkey_challenge = rate_limit_per_ip(
    "passkey_challenge", lambda: settings.passkey_challenge_rate_limit_per_minute, 60
)
rate_limit_passkey_open = rate_limit_per_ip(
    "passkey_open", lambda: settings.passkey_open_rate_limit_per_minute, 60
)


def _caller(request: Request) -> str:
    return request.state.jwt_data.nostr_pubkey


@public_router.post(
    path="/challenge",
    dependencies=[Depends(rate_limit_passkey_challenge)],
    summary="A single-use challenge for opening a passkey vault",
)
async def passkey_vault_challenge_endpoint() -> PasskeyVaultChallengeResponse:
    return PasskeyVaultChallengeResponse(
        data=PasskeyVaultChallenge(challenge=issue_challenge())
    )


@public_router.post(
    path="/{credential_id_hash}/open",
    dependencies=[Depends(rate_limit_passkey_open)],
    summary="Hand a passkey's vault to an assertion from that passkey",
)
async def open_passkey_vault_endpoint(
    body: OpenPasskeyVaultBody,
    credential_id_hash: str = CREDENTIAL_ID_HASH,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> PasskeyVaultBlobResponse:
    blob = await open_vault(
        db,
        path_hash=credential_id_hash,
        credential_id=body.assertion.credential_id,
        client_data_json=body.assertion.client_data_json,
        authenticator_data=body.assertion.authenticator_data,
        signature=body.assertion.signature,
    )
    return PasskeyVaultBlobResponse(data=PasskeyVaultBlob(blob=blob))


@router.get(path="", summary="The caller's passkeys, without their vaults")
async def list_passkey_vaults_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> PasskeyVaultListResponse:
    return PasskeyVaultListResponse(data=await list_vaults(db, _caller(request)))


@router.put(
    path="/{credential_id_hash}",
    summary="Store a passkey-locked copy of the caller's key",
)
async def put_passkey_vault_endpoint(
    request: Request,
    body: PutPasskeyVaultBody,
    credential_id_hash: str = CREDENTIAL_ID_HASH,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> PasskeyVaultEntryResponse:
    entry = await store_vault(
        db,
        caller_pubkey=_caller(request),
        path_hash=credential_id_hash,
        credential_id=body.credential_id,
        public_key=body.public_key,
        alg=body.alg,
        blob=body.blob,
        label=body.label,
    )
    return PasskeyVaultEntryResponse(data=entry)


@router.delete(
    path="/{credential_id_hash}", summary="Remove one of the caller's passkey vaults"
)
async def delete_passkey_vault_endpoint(
    request: Request,
    credential_id_hash: str = CREDENTIAL_ID_HASH,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> PasskeyVaultDeletedResponse:
    await delete_vault(db, _caller(request), credential_id_hash)
    return PasskeyVaultDeletedResponse()
