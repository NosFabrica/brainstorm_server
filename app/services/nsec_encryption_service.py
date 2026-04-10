"""Nsec encryption key rotation and verification.

Runs in-process. The rotation flow mirrors the previous out-of-band script:

    1. Pre-flight verify every row decrypts under the current keys.
    2. Atomic-write [new, old] → reload → re-encrypt all rows with
       MultiFernet([new, old]).
    3. Verify every row decrypts with [new] alone.
    4. Atomic-write [new] → reload. Rotation complete.

Abort safety: any failure before step 2 leaves the key file untouched.
A verify failure after step 2 leaves the key file as [new, old], so the
app keeps working and the operator can investigate.
"""

import asyncio
from dataclasses import dataclass

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import func, select, update

from app.core.database import db_session
from app.core.loggr import loggr
from app.db_models import BrainstormNsec
from app.utils.encryption import (
    current_keys,
    load_keys_from_file,
    write_keys_to_file,
)

logger = loggr.get_logger(__name__)


class RotationFailed(Exception):
    pass


@dataclass
class VerifyResult:
    ok: int
    fail: int


_rotation_lock = asyncio.Lock()


def is_rotation_running() -> bool:
    return _rotation_lock.locked()


async def _verify_with(mf: MultiFernet) -> VerifyResult:
    ok, fail = 0, 0
    async with db_session() as db:
        rows = (
            await db.execute(
                select(BrainstormNsec.pubkey, BrainstormNsec.encrypted_nsec).where(
                    BrainstormNsec.encrypted_nsec.is_not(None)
                )
            )
        ).all()
    for _pubkey, ciphertext in rows:
        try:
            mf.decrypt(ciphertext.encode())
            ok += 1
        except Exception:
            fail += 1
    return VerifyResult(ok=ok, fail=fail)


async def _reencrypt_all(mf: MultiFernet) -> int:
    async with db_session() as db:
        rows = (
            await db.execute(
                select(BrainstormNsec.pubkey, BrainstormNsec.encrypted_nsec).where(
                    BrainstormNsec.encrypted_nsec.is_not(None)
                )
            )
        ).all()
        updated = 0
        for pubkey, ciphertext in rows:
            plaintext = mf.decrypt(ciphertext.encode()).decode()
            new_enc = mf.encrypt(plaintext.encode()).decode()
            await db.execute(
                update(BrainstormNsec)
                .where(BrainstormNsec.pubkey == pubkey)
                .values(encrypted_nsec=new_enc)
            )
            updated += 1
    return updated


async def bootstrap_keys() -> None:
    """Load keys from disk, or generate a fresh one if the file is empty.

    Called at app startup. Refuses to mint a new key if ciphertext rows
    already exist — that would strand them. Operator must restore the
    key file from the secrets vault instead.
    """
    loaded = load_keys_from_file()
    if loaded > 0:
        logger.info(f"nsec encryption: loaded {loaded} key(s) from file")
        return

    if await count_encrypted_rows() > 0:
        logger.error(
            "nsec encryption: key file missing but encrypted_nsec rows exist; "
            "refusing to bootstrap a new key. Restore the key file from your "
            "secrets vault. Requests that decrypt nsecs will fail until fixed."
        )
        return

    logger.info("nsec encryption: no key file found, generating new key")
    write_keys_to_file([Fernet.generate_key().decode()])
    load_keys_from_file()
    encrypted = await encrypt_plaintext_rows()
    logger.info(
        f"nsec encryption: bootstrapped new key; encrypted {encrypted} "
        f"pre-existing plaintext rows"
    )


async def count_encrypted_rows() -> int:
    """Rows already holding ciphertext — used to guard against bootstrapping
    over an existing key file that got lost."""
    async with db_session() as db:
        return (
            await db.execute(
                select(func.count()).select_from(BrainstormNsec).where(
                    BrainstormNsec.encrypted_nsec.is_not(None)
                )
            )
        ).scalar_one()


async def encrypt_plaintext_rows() -> int:
    """Bootstrap path: populate encrypted_nsec for rows that only have plaintext."""
    mf = MultiFernet([Fernet(k) for k in current_keys()])
    async with db_session() as db:
        rows = (
            await db.execute(
                select(BrainstormNsec.pubkey, BrainstormNsec.nsec).where(
                    BrainstormNsec.encrypted_nsec.is_(None),
                    BrainstormNsec.nsec.is_not(None),
                )
            )
        ).all()
        updated = 0
        for pubkey, plaintext in rows:
            new_enc = mf.encrypt(plaintext.encode()).decode()
            await db.execute(
                update(BrainstormNsec)
                .where(BrainstormNsec.pubkey == pubkey)
                .values(encrypted_nsec=new_enc)
            )
            updated += 1
    return updated


async def verify_keys() -> VerifyResult:
    keys = current_keys()
    if not keys:
        raise RotationFailed("no keys loaded")
    mf = MultiFernet([Fernet(keys[0])])
    return await _verify_with(mf)


async def rotate_key() -> None:
    async with _rotation_lock:
        existing = current_keys()
        if not existing:
            raise RotationFailed("no current key loaded; cannot rotate")

        current_mf = MultiFernet([Fernet(k) for k in existing])
        pre = await _verify_with(current_mf)
        if pre.fail > 0:
            raise RotationFailed(
                f"pre-flight: {pre.fail} of {pre.ok + pre.fail} rows do not decrypt "
                f"with current keys; refusing to rotate"
            )
        logger.info(f"nsec rotate: pre-flight ok ({pre.ok} rows)")

        old = existing[0]
        new = Fernet.generate_key().decode()

        write_keys_to_file([new, old])
        load_keys_from_file()
        logger.info("nsec rotate: wrote [new, old] and reloaded")

        try:
            both = MultiFernet([Fernet(new), Fernet(old)])
            count = await _reencrypt_all(both)
            logger.info(f"nsec rotate: re-encrypted {count} rows")

            new_only = MultiFernet([Fernet(new)])
            post = await _verify_with(new_only)
            if post.fail > 0:
                raise RotationFailed(
                    f"post-rotate verify: {post.fail} rows do not decrypt with new key; "
                    f"key file left at [new, old] for investigation"
                )
            logger.info(f"nsec rotate: verify ok ({post.ok} rows)")
        except Exception:
            logger.exception(
                "nsec rotate: failure after dual-key write; key file remains [new, old]"
            )
            raise

        write_keys_to_file([new])
        load_keys_from_file()
        logger.info("nsec rotate: wrote [new] and reloaded — rotation complete")
