"""Deterministic NIP-05 identifiers for Brainstorm Assistant pubkeys."""

import hashlib
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import settings

_WORDLIST_FILE = Path(__file__).with_name("bip39_english.txt")

# Standard BIP-39 English wordlist, vendored so this adds no dependency.
BIP39_ENGLISH_WORDS: tuple[str, ...] = tuple(
    _WORDLIST_FILE.read_text(encoding="utf-8").split()
)


def compute_assistant_nip05_local_part(pubkey_hex: str) -> str:
    """Derive the ``<word1>_<word2>_<hex4>`` local-part for an Assistant pubkey."""
    # Hash first so the wordlist positions stay uniformly distributed.
    digest = hashlib.sha256(bytes.fromhex(pubkey_hex)).digest()

    wordlist_size = len(BIP39_ENGLISH_WORDS)
    word1 = BIP39_ENGLISH_WORDS[int.from_bytes(digest[0:2], "big") % wordlist_size]
    word2 = BIP39_ENGLISH_WORDS[int.from_bytes(digest[2:4], "big") % wordlist_size]
    hex4 = digest[4:6].hex()

    return f"{word1}_{word2}_{hex4}"


def compute_assistant_nip05(pubkey_hex: str) -> str | None:
    """Full ``<local-part>@<domain>`` identifier off FRONTEND_URL's hostname.

    ``None`` means that env var carries no hostname; callers omit the field.
    """
    domain = urlparse(settings.frontend_url).hostname
    if not domain:
        return None

    return f"{compute_assistant_nip05_local_part(pubkey_hex)}@{domain}"
