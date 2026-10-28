from datetime import datetime
from typing import cast

from sqlalchemy import CursorResult, RowMapping, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
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
    credential_id_hash: str,
    credential_id: str,
    pubkey: str,
    credential_public_key: str,
    alg: int,
    blob: str,
    label: str | None,
) -> RowMapping | None:
    """Write the owner's row in one statement; None when the hash is someone else's.

    One statement, so two first uploads of one passkey (a double submit, two
    tabs) settle in the database instead of the second tripping the primary key.
    An owner's rewrite resets the signature counter: it is their only way out of
    a counter an authenticator stopped keeping.
    """
    values = insert(PasskeyVault).values(
        credential_id_hash=credential_id_hash,
        credential_id=credential_id,
        pubkey=pubkey,
        credential_public_key=credential_public_key,
        alg=alg,
        blob=blob,
        label=label,
    )
    stmt = values.on_conflict_do_update(
        index_elements=[PasskeyVault.credential_id_hash],
        set_={
            "credential_public_key": values.excluded.credential_public_key,
            "alg": values.excluded.alg,
            "blob": values.excluded.blob,
            "label": values.excluded.label,
            "sign_count": 0,
            "updated_at": func.now(),
        },
        where=PasskeyVault.pubkey == values.excluded.pubkey,
    ).returning(
        PasskeyVault.credential_id_hash,
        PasskeyVault.credential_id,
        PasskeyVault.label,
        PasskeyVault.created_at,
        PasskeyVault.last_used_at,
    )
    result = await db.execute(stmt)
    return result.mappings().one_or_none()


async def mark_passkey_vault_used_on_db(
    db: AsyncDBSession, credential_id_hash: str, sign_count: int, used_at: datetime
) -> bool:
    """Record an open, only if the counter moves forward — or both sides keep none.

    Conditional in the database, so two opens racing can't write an older count
    over a newer one. False when the counter didn't advance: a cloned
    authenticator, or one replaying an old assertion.
    """
    advanced = (
        PasskeyVault.sign_count == 0
        if sign_count == 0
        else PasskeyVault.sign_count < sign_count
    )
    result = await db.execute(
        update(PasskeyVault)
        .where(PasskeyVault.credential_id_hash == credential_id_hash, advanced)
        .values(sign_count=sign_count, last_used_at=used_at)
    )
    # DML results are CursorResult at runtime; the base Result stub lacks rowcount.
    return cast(CursorResult, result).rowcount == 1


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
