"""
Rotate the nsec encryption key with zero downtime.

Run via:
    docker compose --profile ops run --rm nsec-rotate

Flow:
    1. Read current key file.
    2. Generate new Fernet key.
    3. Atomic write <new>,<old> → SIGUSR1 app → app reloads, accepts either.
    4. Re-encrypt all rows using in-process MultiFernet([new, old]).
    5. Verify every row decrypts with <new> alone.
    6. Atomic write <new> → SIGUSR1 app → app drops old key.

Abort safety: any failure before step 4 leaves the file with both keys and the
app still working. Verify failure (step 5) leaves the file with both keys so the
run can be retried after investigation.
"""

import argparse
import asyncio
import http.client
import os
import socket
import sys
import tempfile
from pathlib import Path

import asyncpg
from cryptography.fernet import Fernet, MultiFernet

DEFAULT_KEY_FILE = Path("/run/secrets/nsec_encryption_keys")
DEFAULT_CONTAINER = "brainstorm-server"
DEFAULT_DOCKER_SOCK = "/var/run/docker.sock"
LOCK_FILE = Path("/tmp/nsec-rotate.lock")


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


def docker_signal(container: str, sig: str, sock_path: str) -> None:
    conn = UnixHTTPConnection(sock_path)
    try:
        conn.request("POST", f"/containers/{container}/kill?signal={sig}")
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 204:
            raise RuntimeError(f"docker kill {container} {sig} failed: {resp.status} {body!r}")
    finally:
        conn.close()


def read_keys(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw = path.read_text().strip()
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def atomic_write_keys(path: Path, keys: list[str]) -> None:
    content = ",".join(keys)
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".rotate.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def acquire_lock() -> None:
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        print(f"ERROR: lock file {LOCK_FILE} exists. Another rotation in progress, or stale lock.")
        sys.exit(1)


def release_lock() -> None:
    try:
        os.unlink(LOCK_FILE)
    except FileNotFoundError:
        pass


def asyncpg_dsn() -> str:
    url = os.environ.get("DB_URL") or os.environ.get("db_url")
    if not url:
        print("ERROR: DB_URL env var not set.")
        sys.exit(1)
    # strip SQLAlchemy driver suffix if present
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql+aiopg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


async def reencrypt_rows(conn: asyncpg.Connection, mf: MultiFernet, dry_run: bool) -> int:
    """Decrypt every populated `encrypted_nsec` with `mf` and write it back.

    Pre-flight guarantees every row is decryptable before this is called, so
    there is no fallback path here — a decrypt failure is a bug.
    """
    rows = await conn.fetch(
        "SELECT pubkey, encrypted_nsec FROM brainstorm_nsec WHERE encrypted_nsec IS NOT NULL"
    )
    updated = 0
    for row in rows:
        plaintext = mf.decrypt(row["encrypted_nsec"].encode()).decode()
        new_enc = mf.encrypt(plaintext.encode()).decode()
        if dry_run:
            updated += 1
            continue
        await conn.execute(
            "UPDATE brainstorm_nsec SET encrypted_nsec = $1 WHERE pubkey = $2",
            new_enc, row["pubkey"],
        )
        updated += 1
    return updated


async def encrypt_from_plaintext(conn: asyncpg.Connection, mf: MultiFernet) -> int:
    """Bootstrap-only: encrypt from the legacy `nsec` column into `encrypted_nsec`."""
    rows = await conn.fetch(
        "SELECT pubkey, nsec FROM brainstorm_nsec WHERE nsec IS NOT NULL"
    )
    updated = 0
    for row in rows:
        new_enc = mf.encrypt(row["nsec"].encode()).decode()
        await conn.execute(
            "UPDATE brainstorm_nsec SET encrypted_nsec = $1 WHERE pubkey = $2",
            new_enc, row["pubkey"],
        )
        updated += 1
    return updated


async def verify_rows(conn: asyncpg.Connection, new_only: MultiFernet) -> tuple[int, int]:
    rows = await conn.fetch(
        "SELECT pubkey, encrypted_nsec FROM brainstorm_nsec WHERE encrypted_nsec IS NOT NULL"
    )
    ok, fail = 0, 0
    for row in rows:
        try:
            new_only.decrypt(row["encrypted_nsec"].encode())
            ok += 1
        except Exception:
            fail += 1
    return ok, fail


async def run(args) -> int:
    conn = await asyncpg.connect(asyncpg_dsn())
    try:
        existing = read_keys(args.key_file)

        if args.verify_only:
            if not existing:
                print("ERROR: no keys in file, nothing to verify.")
                return 1
            only = MultiFernet([Fernet(existing[0])])
            ok, fail = await verify_rows(conn, only)
            print(f"verify: {ok} ok, {fail} fail (using first key only)")
            return 0 if fail == 0 else 2

        # Pre-flight: if we have a key, every existing ciphertext must decrypt
        # under it before we mutate anything. Bails out before the first write
        # if state is already drifted.
        if existing:
            current_mf = MultiFernet([Fernet(k) for k in existing])
            ok, fail = await verify_rows(conn, current_mf)
            if fail > 0:
                print(
                    f"PRE-FLIGHT FAIL: {fail} of {ok + fail} rows do not decrypt "
                    f"with current key(s). Refusing to rotate. Investigate stale "
                    f"ciphertext before retrying."
                )
                return 3
            print(f"Pre-flight: {ok} rows decrypt cleanly with current key(s).")

        old = existing[0] if existing else None
        new = Fernet.generate_key().decode()

        if old is None:
            print("No existing key found — bootstrapping with new key only.")
            if args.dry_run:
                print(f"[dry-run] would write single key to {args.key_file}")
                return 0
            atomic_write_keys(args.key_file, [new])
            print(f"Wrote new key to {args.key_file}.")
            try:
                docker_signal(args.container, "SIGUSR1", args.docker_sock)
                print(f"Sent SIGUSR1 to {args.container}.")
            except Exception as e:
                print(f"WARN: docker signal failed: {e}. App may need manual restart.")
            mf = MultiFernet([Fernet(new)])
            count = await encrypt_from_plaintext(conn, mf)
            print(f"Bootstrap done. Encrypted {count} rows from plaintext column.")
            return 0

        print(f"Rotating. Current keys in file: {len(existing)}")

        if args.dry_run:
            print(f"[dry-run] would write [new, old] to {args.key_file}")
            mf = MultiFernet([Fernet(new), Fernet(old)])
            count = await reencrypt_rows(conn, mf, dry_run=True)
            print(f"[dry-run] would re-encrypt {count} rows")
            print(f"[dry-run] would verify with new key only")
            print(f"[dry-run] would write [new] to {args.key_file}")
            return 0

        atomic_write_keys(args.key_file, [new, old])
        print("Wrote [new, old] to key file.")
        docker_signal(args.container, "SIGUSR1", args.docker_sock)
        print(f"Sent SIGUSR1 to {args.container}.")
        await asyncio.sleep(1)

        mf_both = MultiFernet([Fernet(new), Fernet(old)])
        count = await reencrypt_rows(conn, mf_both, dry_run=False)
        print(f"Re-encrypted {count} rows.")

        mf_new = MultiFernet([Fernet(new)])
        ok, fail = await verify_rows(conn, mf_new)
        print(f"Verify: {ok} ok, {fail} fail.")
        if fail > 0:
            print("ABORT: verify failed. Key file still has [new, old], app still works. Investigate.")
            return 2

        atomic_write_keys(args.key_file, [new])
        print("Wrote [new] to key file.")
        docker_signal(args.container, "SIGUSR1", args.docker_sock)
        print(f"Sent SIGUSR1 to {args.container}. Rotation complete.")
        return 0
    finally:
        await conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    p.add_argument("--container", default=DEFAULT_CONTAINER)
    p.add_argument("--docker-sock", default=DEFAULT_DOCKER_SOCK)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verify-only", action="store_true")
    args = p.parse_args()

    acquire_lock()
    try:
        return asyncio.run(run(args))
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
