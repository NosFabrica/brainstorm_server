"""The passkey vault: passkey-locked key copies, for signing in on a new device.

The server holds ciphertext it cannot open and the public key of the passkey
that can. Writing and removing a row takes a Session (only the key's owner can
have one); reading a row back takes a fresh assertion from the passkey itself,
since the reader is by definition a device that has no Session yet.

Why the read is gated at all, when the blob is ciphertext: an attacker who takes
over someone's Apple or Google account holds their synced passkey. A freely
fetchable blob would hand them the other half; behind an assertion over our own
challenge, getting it takes the same live ceremony that produces the key.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.loggr import loggr
from app.core.redis_db import get_redis_client
from app.repos.passkey_vault_repo import (
    delete_passkey_vault_on_db,
    mark_passkey_vault_used_on_db,
    select_passkey_vault_by_hash_on_db,
    select_passkey_vaults_by_pubkey_on_db,
    upsert_passkey_vault_on_db,
)
from app.schemas.schemas import PasskeyVaultEntry
from app.utils.webauthn import (
    SUPPORTED_ALGS,
    AssertionInvalid,
    b64url_decode,
    credential_id_hash,
    is_canonical_b64url,
    load_public_key,
    verify_assertion,
)

logger = loggr.get_logger(__name__)

CHALLENGE_TTL_SECONDS = 120
CLOCK_SKEW_SECONDS = 5
_NONCE_BYTES = 16
_USED_PREFIX = "passkey_challenge_used:"


def _challenge_key() -> bytes:
    # Derived rather than configured: one less secret to provision, and rotating
    # the auth secret already invalidates everything a challenge could unlock.
    return hmac.new(
        settings.auth_secret_key.encode(), b"passkey-vault-challenge", hashlib.sha256
    ).digest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def issue_challenge(now: float | None = None) -> str:
    """Self-authenticating: nonce ‖ issued-at ‖ HMAC, so issuing one costs nothing."""
    nonce = secrets.token_bytes(_NONCE_BYTES)
    issued = int(now if now is not None else time.time()).to_bytes(8, "big")
    mac = hmac.new(_challenge_key(), nonce + issued, hashlib.sha256).digest()
    return _b64url(nonce + issued + mac)


def check_challenge(challenge: str, now: float | None = None) -> bytes:
    """Ours and unexpired: its nonce, or ``AssertionInvalid``. Reads nothing, writes nothing."""
    raw = b64url_decode(challenge)
    if len(raw) != _NONCE_BYTES + 8 + 32:
        raise AssertionInvalid("challenge has the wrong shape")
    nonce, issued, mac = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:-32], raw[-32:]
    expected = hmac.new(_challenge_key(), nonce + issued, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise AssertionInvalid("challenge is not ours")
    age = (now if now is not None else time.time()) - int.from_bytes(issued, "big")
    # Issued by one replica and redeemed by another: a clock a little behind
    # must not refuse a challenge that is seconds old.
    if age < -CLOCK_SKEW_SECONDS or age > CHALLENGE_TTL_SECONDS:
        raise AssertionInvalid("challenge expired")
    return nonce


async def spend_challenge(nonce: bytes) -> None:
    """Spend a checked challenge. ``SET NX`` makes the first spend the only one."""
    spent = await get_redis_client().set(
        f"{_USED_PREFIX}{nonce.hex()}", "1", nx=True, ex=CHALLENGE_TTL_SECONDS + 60
    )
    if not spent:
        raise AssertionInvalid("challenge already used")


async def redeem_challenge(challenge: str, now: float | None = None) -> None:
    """Ours, unexpired and unspent — then spent. Raises ``AssertionInvalid`` otherwise."""
    await spend_challenge(check_challenge(challenge, now))


def allowed_origins() -> list[str]:
    configured = [o.strip().rstrip("/") for o in settings.passkey_origins.split(",")]
    origins = [o for o in configured if o]
    return origins or [settings.frontend_url.rstrip("/")]


def _not_found() -> HTTPException:
    # One answer for every failure on the unauthenticated read: no oracle for
    # "no such row" against "bad signature" against "spent challenge".
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _entry(row) -> PasskeyVaultEntry:
    return PasskeyVaultEntry(
        credential_id_hash=row.credential_id_hash,
        credential_id=row.credential_id,
        label=row.label,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


def _validate_blob(blob: str, pubkey: str, credential_id: str) -> None:
    """Shape and ownership only (size is the request schema's). The contents are
    the client's, and stay sealed."""
    try:
        record = json.loads(blob)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "blob is not JSON")
    if not isinstance(record, dict) or record.get("v") != 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown blob version"
        )
    # A row may only carry a key for its own owner, under its own credential —
    # otherwise one account could plant a vault that another's passkey opens.
    if record.get("pubkey") != pubkey or record.get("credentialId") != credential_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "blob does not match this passkey"
        )


async def store_vault(
    db: AsyncDBSession,
    *,
    caller_pubkey: str,
    path_hash: str,
    credential_id: str,
    public_key: str,
    alg: int,
    blob: str,
    label: str | None,
) -> PasskeyVaultEntry:
    try:
        # Canonical, because the blob's credentialId is compared as a string
        # while the row key hashes the bytes: two spellings, one passkey.
        if not is_canonical_b64url(credential_id):
            raise AssertionInvalid("credentialId is not canonical base64url")
        if credential_id_hash(credential_id) != path_hash:
            raise AssertionInvalid("credentialId does not match the path")
        if alg not in SUPPORTED_ALGS:
            raise AssertionInvalid("unsupported alg")
        load_public_key(public_key, alg)
    except AssertionInvalid as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))
    _validate_blob(blob, caller_pubkey, credential_id)

    row = await upsert_passkey_vault_on_db(
        db,
        credential_id_hash=path_hash,
        credential_id=credential_id,
        pubkey=caller_pubkey,
        credential_public_key=public_key,
        alg=alg,
        blob=blob,
        label=label,
    )
    if row is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This passkey belongs to another account"
        )
    return PasskeyVaultEntry(
        credential_id_hash=row["credential_id_hash"],
        credential_id=row["credential_id"],
        label=row["label"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


async def list_vaults(
    db: AsyncDBSession, caller_pubkey: str
) -> list[PasskeyVaultEntry]:
    return [
        _entry(row)
        for row in await select_passkey_vaults_by_pubkey_on_db(db, caller_pubkey)
    ]


async def open_vault(
    db: AsyncDBSession,
    *,
    path_hash: str,
    credential_id: str,
    client_data_json: str,
    authenticator_data: str,
    signature: str,
) -> str:
    try:
        if credential_id_hash(credential_id) != path_hash:
            raise AssertionInvalid("assertion addresses another row")
        row = await select_passkey_vault_by_hash_on_db(db, path_hash)
        if row is None:
            raise AssertionInvalid("no such row")
        verified = verify_assertion(
            client_data_json=client_data_json,
            authenticator_data=authenticator_data,
            signature=signature,
            public_key=row.credential_public_key,
            alg=row.alg,
            allowed_origins=allowed_origins(),
            check_challenge=check_challenge,
        )
        # Counters only mean anything when the authenticator keeps one; synced
        # passkeys report 0 forever. A counter that went backwards is a clone.
        # Checked here to fail before spending anything; enforced again, race-free,
        # by the conditional update below.
        if (
            verified.sign_count or row.sign_count
        ) and verified.sign_count <= row.sign_count:
            raise AssertionInvalid("signature counter did not advance")
        # Spent after the signature, so a forged assertion can't burn someone
        # else's challenge.
        await spend_challenge(check_challenge(verified.challenge))
        if not await mark_passkey_vault_used_on_db(db, path_hash, verified.sign_count):
            raise AssertionInvalid("signature counter did not advance (raced)")
    except AssertionInvalid as error:
        logger.info("passkey vault open refused: %s", error)
        raise _not_found()

    return row.blob


async def delete_vault(db: AsyncDBSession, caller_pubkey: str, path_hash: str) -> None:
    await delete_passkey_vault_on_db(db, path_hash, caller_pubkey)
