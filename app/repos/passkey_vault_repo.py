from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.db_models import PasskeyVault


async def select_passkey_vault_by_hash_on_db(
    db: AsyncDBSession, credential_id_hash: str
) -> PasskeyVault | None:
    result = await db.execute(
        select(PasskeyVault).where(
            PasskeyVault.credential_id_hash == credential_id_hash
        )
    )
    return result.scalar_one_or_none()


async def select_passkey_vaults_by_pubkey_on_db(
    db: AsyncDBSession, pubkey: str
) -> list[PasskeyVault]:
    result = await db.execute(
        select(PasskeyVault)
        .where(PasskeyVault.pubkey == pubkey)
        .order_by(PasskeyVault.created_at)
    )
    return list(result.scalars().all())


async def upsert_passkey_vault_on_db(
    db: AsyncDBSession,
    *,
    existing: PasskeyVault | None,
    credential_id_hash: str,
    pubkey: str,
    credential_public_key: str,
    alg: int,
    blob: str,
    label: str | None,
) -> PasskeyVault:
    """Write a row, keeping the signature counter while the credential key is unchanged."""
    if existing is None:
        row = PasskeyVault(
            credential_id_hash=credential_id_hash,
            pubkey=pubkey,
            credential_public_key=credential_public_key,
            alg=alg,
            blob=blob,
            label=label,
        )
        db.add(row)
    else:
        row = existing
        if row.credential_public_key != credential_public_key or row.alg != alg:
            row.sign_count = 0
        row.credential_public_key = credential_public_key
        row.alg = alg
        row.blob = blob
        row.label = label
    await db.flush()
    return row


async def mark_passkey_vault_used_on_db(
    db: AsyncDBSession, row: PasskeyVault, sign_count: int, used_at: datetime
) -> None:
    row.sign_count = sign_count
    row.last_used_at = used_at
    await db.flush()


async def delete_passkey_vault_on_db(
    db: AsyncDBSession, credential_id_hash: str, pubkey: str
) -> None:
    """Only the owner's row goes; anyone else's hash deletes nothing, silently."""
    await db.execute(
        delete(PasskeyVault).where(
            PasskeyVault.credential_id_hash == credential_id_hash,
            PasskeyVault.pubkey == pubkey,
        )
    )
