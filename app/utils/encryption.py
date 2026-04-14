import os
import tempfile
import threading
from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet

KEY_FILE_PATH = Path("/run/secrets/nsec_encryption_keys")

_mf: MultiFernet | None = None
_keys: list[str] = []
_lock = threading.Lock()


def _parse_keys(raw: str) -> list[str]:
    return [k.strip() for k in raw.split(",") if k.strip()]


def read_keys_from_file(path: Path = KEY_FILE_PATH) -> list[str]:
    try:
        raw = path.read_text().strip()
    except FileNotFoundError:
        return []
    return _parse_keys(raw) if raw else []


def write_keys_to_file(keys: list[str], path: Path = KEY_FILE_PATH) -> None:
    """Atomic write of comma-joined keys to path (0600)."""
    content = ",".join(keys)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".keys.", suffix=".tmp")
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


def load_keys_from_file(path: Path = KEY_FILE_PATH) -> int:
    """(Re)load keys from disk into the in-process MultiFernet. Returns key count."""
    global _mf, _keys
    keys = read_keys_from_file(path)
    with _lock:
        _keys = keys
        _mf = MultiFernet([Fernet(k) for k in keys]) if keys else None
    return len(keys)


def current_keys() -> list[str]:
    with _lock:
        return list(_keys)


def get_mf() -> MultiFernet | None:
    with _lock:
        return _mf


def is_encryption_configured() -> bool:
    return get_mf() is not None


def encrypt_nsec(plaintext: str) -> str:
    mf = get_mf()
    if mf is None:
        return plaintext
    return mf.encrypt(plaintext.encode()).decode()


def decrypt_nsec(value: str) -> str:
    if value.startswith("nsec1"):
        return value
    mf = get_mf()
    if mf is None:
        return value
    return mf.decrypt(value.encode()).decode()
