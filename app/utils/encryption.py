import threading
from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet

KEY_FILE_PATH = Path("/run/secrets/nsec_encryption_keys")

_cached_mf: MultiFernet | None = None
_cache_loaded: bool = False
_lock = threading.Lock()


def _load_from_file() -> MultiFernet | None:
    try:
        raw = KEY_FILE_PATH.read_text().strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    keys = [Fernet(k.strip()) for k in raw.split(",") if k.strip()]
    if not keys:
        return None
    return MultiFernet(keys)


def _get_multi_fernet() -> MultiFernet | None:
    global _cached_mf, _cache_loaded
    if _cache_loaded:
        return _cached_mf
    with _lock:
        if not _cache_loaded:
            _cached_mf = _load_from_file()
            _cache_loaded = True
    return _cached_mf


def reload_keys() -> None:
    """Clear cached MultiFernet; next call reloads from file. SIGUSR1 handler."""
    global _cached_mf, _cache_loaded
    with _lock:
        _cached_mf = None
        _cache_loaded = False


def is_encryption_configured() -> bool:
    return _get_multi_fernet() is not None


def encrypt_nsec(plaintext: str) -> str:
    mf = _get_multi_fernet()
    if mf is None:
        return plaintext
    return mf.encrypt(plaintext.encode()).decode()


def decrypt_nsec(value: str) -> str:
    if value.startswith("nsec1"):
        return value
    mf = _get_multi_fernet()
    if mf is None:
        return value
    return mf.decrypt(value.encode()).decode()
