"""NIP-44 v2 between the Assistant's key and one peer, through nostr-sdk.

nostr-sdk's own implementation is the spec's (its test vectors pass, see
`tests/test_nip44.py`), so a ciphertext made here opens in any NIP-44 client
(Quartz, nostr-tools) and theirs open here. Encryption is symmetric between two
keys: what A encrypts to B, B decrypts with A as peer, and A can decrypt it too.

A failure raises `Nip44Error`, whose message is fixed and never carries the
plaintext, the ciphertext or nostr-sdk's own error text (`from None`, so the
traceback doesn't either): the callers handle AI keys.
"""
from __future__ import annotations

from nostr_sdk import (  # type: ignore
    Keys,
    Nip44Version,
    PublicKey,
    nip44_decrypt,
    nip44_encrypt,
)


class Nip44Error(Exception):
    """A NIP-44 step failed. The message says which step, nothing more."""


def _peer(peer_hex: str) -> PublicKey:
    try:
        return PublicKey.parse(peer_hex)
    except Exception:
        raise Nip44Error("not a public key") from None


def encrypt(keys: Keys, peer_hex: str, plaintext: str) -> str:
    """`plaintext` encrypted from `keys` to `peer_hex` (NIP-44 v2, base64)."""
    peer = _peer(peer_hex)
    try:
        return nip44_encrypt(keys.secret_key(), peer, plaintext, Nip44Version.V2)
    except Exception:
        raise Nip44Error("could not encrypt") from None


def decrypt(keys: Keys, peer_hex: str, ciphertext: str) -> str:
    """The plaintext of a NIP-44 v2 `ciphertext` between `keys` and `peer_hex`."""
    peer = _peer(peer_hex)
    try:
        return nip44_decrypt(keys.secret_key(), peer, ciphertext)
    except Exception:
        raise Nip44Error("did not decrypt") from None
