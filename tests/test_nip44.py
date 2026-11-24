"""NIP-44 v2 (`app/utils/nip44.py`): what the server encrypts opens in any
NIP-44 client, and what they encrypt opens here.

Two independent checks, since nostr-sdk takes no nonce and so can't reproduce a
spec payload byte for byte:

- nostr-sdk decrypts the spec's own payloads (`nip44.vectors.json`, v2 valid
  `encrypt_decrypt`, as Quartz and nostr-tools test against).
- A reference implementation written from the spec here, on `cryptography`
  alone, reproduces those payloads byte for byte; it then decrypts what the
  server encrypts, and the server decrypts what it encrypts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import math
import os

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from nostr_sdk import Keys

from app.utils import nip44

# From the NIP-44 spec's nip44.vectors.json, v2.valid.encrypt_decrypt.
SPEC_VECTORS = [
    {
        "sec1": "0000000000000000000000000000000000000000000000000000000000000001",
        "sec2": "0000000000000000000000000000000000000000000000000000000000000002",
        "conversation_key": "c41c775356fd92eadc63ff5a0dc1da211b268cbea22316767095b2871ea1412d",
        "nonce": "0000000000000000000000000000000000000000000000000000000000000001",
        "plaintext": "a",
        "payload": "AgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABee0G5VSK0/9YypIObAtDKfYEAjD35uVkHyB0F4DwrcNaCXlCWZKaArsGrY6M9wnuTMxWfp1RTN9Xga8no+kF5Vsb",
    },
    {
        "sec1": "0000000000000000000000000000000000000000000000000000000000000002",
        "sec2": "0000000000000000000000000000000000000000000000000000000000000001",
        "conversation_key": "c41c775356fd92eadc63ff5a0dc1da211b268cbea22316767095b2871ea1412d",
        "nonce": "f00000000000000000000000000000f00000000000000000000000000000000f",
        "plaintext": "\U0001f355\U0001fac3",
        "payload": "AvAAAAAAAAAAAAAAAAAAAPAAAAAAAAAAAAAAAAAAAAAPSKSK6is9ngkX2+cSq85Th16oRTISAOfhStnixqZziKMDvB0QQzgFZdjLTPicCJaV8nDITO+QfaQ61+KbWQIOO2Yj",
    },
    {
        "sec1": "8f40e50a84a7462e2b8d24c28898ef1f23359fff50d8c509e6fb7ce06e142f9c",
        "sec2": "b9b0a1e9cc20100c5faa3bbe2777303d25950616c4c6a3fa2e3e046f936ec2ba",
        "conversation_key": "d5a2f879123145a4b291d767428870f5a8d9e5007193321795b40183d4ab8c2b",
        "nonce": "b20989adc3ddc41cd2c435952c0d59a91315d8c5218d5040573fc3749543acaf",
        "plaintext": "ability\U0001f91d的 ȺȾ",
        "payload": "ArIJia3D3cQc0sQ1lSwNWakTFdjFIY1QQFc/w3SVQ6yvbG2S0x4Yu86QGwPTy7mP3961I1XqB6SFFTzqDZZavhxoWMj7mEVGMQIsh2RLWI5EYQaQDIePSnXPlzf7CIt+voTD",
    },
]


# --- A reference NIP-44 v2, from the spec ----------------------------------


def ref_conversation_key(secret_hex: str, peer_x_hex: str) -> bytes:
    private = ec.derive_private_key(int(secret_hex, 16), ec.SECP256K1())
    peer = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256K1(), b"\x02" + bytes.fromhex(peer_x_hex)
    )
    shared_x = private.exchange(ec.ECDH(), peer)
    return hmac.new(b"nip44-v2", shared_x, hashlib.sha256).digest()


def _message_keys(conversation_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    keys = HKDFExpand(hashes.SHA256(), 76, nonce).derive(conversation_key)
    return keys[0:32], keys[32:44], keys[44:76]


def _padded_len(n: int) -> int:
    if n <= 32:
        return 32
    next_power = 1 << (math.floor(math.log2(n - 1)) + 1)
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * (math.floor((n - 1) / chunk) + 1)


def _chacha20(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    # `cryptography` takes the 4-byte little-endian counter, then the nonce.
    cipher = Cipher(algorithms.ChaCha20(key, b"\x00" * 4 + nonce12), mode=None)
    return cipher.encryptor().update(data)


def ref_encrypt(conversation_key: bytes, nonce: bytes, plaintext: str) -> str:
    raw = plaintext.encode()
    padded = (
        len(raw).to_bytes(2, "big") + raw + b"\x00" * (_padded_len(len(raw)) - len(raw))
    )
    chacha_key, chacha_nonce, hmac_key = _message_keys(conversation_key, nonce)
    ciphertext = _chacha20(chacha_key, chacha_nonce, padded)
    mac = hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    return base64.b64encode(b"\x02" + nonce + ciphertext + mac).decode()


def ref_decrypt(conversation_key: bytes, payload: str) -> str:
    data = base64.b64decode(payload)
    assert data[0] == 2
    nonce, ciphertext, mac = data[1:33], data[33:-32], data[-32:]
    chacha_key, chacha_nonce, hmac_key = _message_keys(conversation_key, nonce)
    expected = hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ValueError("invalid MAC")
    padded = _chacha20(chacha_key, chacha_nonce, ciphertext)
    n = int.from_bytes(padded[:2], "big")
    assert len(padded) == 2 + _padded_len(n)
    return padded[2 : 2 + n].decode()


def _pub(secret_hex: str) -> str:
    return Keys.parse(secret_hex).public_key().to_hex()


# --- The spec's vectors ----------------------------------------------------


@pytest.mark.parametrize("v", SPEC_VECTORS, ids=lambda v: v["nonce"][:8])
def test_the_reference_reproduces_the_spec_vectors(v):
    """The reference is the spec: same conversation key, same payload."""
    conversation_key = ref_conversation_key(v["sec1"], _pub(v["sec2"]))
    assert conversation_key.hex() == v["conversation_key"]
    assert (
        ref_encrypt(conversation_key, bytes.fromhex(v["nonce"]), v["plaintext"])
        == v["payload"]
    )


@pytest.mark.parametrize("v", SPEC_VECTORS, ids=lambda v: v["nonce"][:8])
def test_the_server_decrypts_the_spec_payloads_from_either_side(v):
    assert (
        nip44.decrypt(Keys.parse(v["sec2"]), _pub(v["sec1"]), v["payload"])
        == v["plaintext"]
    )
    assert (
        nip44.decrypt(Keys.parse(v["sec1"]), _pub(v["sec2"]), v["payload"])
        == v["plaintext"]
    )


# --- Round trips with a second key pair ------------------------------------


PLAINTEXT = '[["key","sk-or-v1-abc","https://openrouter.ai/api/v1"],["model","x"]]'


def test_what_the_server_encrypts_opens_in_another_client():
    assistant, owner = Keys.generate(), Keys.generate()
    payload = nip44.encrypt(assistant, owner.public_key().to_hex(), PLAINTEXT)

    # The owner's client, with the spec's own algorithm.
    conversation_key = ref_conversation_key(
        owner.secret_key().to_hex(), assistant.public_key().to_hex()
    )
    assert ref_decrypt(conversation_key, payload) == PLAINTEXT
    # And nostr-sdk on the owner's side.
    assert nip44.decrypt(owner, assistant.public_key().to_hex(), payload) == PLAINTEXT


def test_what_another_client_encrypts_opens_on_the_server():
    assistant, owner = Keys.generate(), Keys.generate()
    conversation_key = ref_conversation_key(
        owner.secret_key().to_hex(), assistant.public_key().to_hex()
    )
    payload = ref_encrypt(conversation_key, os.urandom(32), PLAINTEXT)

    assert nip44.decrypt(assistant, owner.public_key().to_hex(), payload) == PLAINTEXT


def test_a_third_key_does_not_decrypt():
    assistant, owner, stranger = Keys.generate(), Keys.generate(), Keys.generate()
    payload = nip44.encrypt(assistant, owner.public_key().to_hex(), PLAINTEXT)

    with pytest.raises(nip44.Nip44Error):
        nip44.decrypt(stranger, assistant.public_key().to_hex(), payload)
    with pytest.raises(nip44.Nip44Error):
        nip44.decrypt(assistant, stranger.public_key().to_hex(), payload)


def test_an_error_carries_neither_the_plaintext_nor_the_payload():
    assistant, owner = Keys.generate(), Keys.generate()
    payload = nip44.encrypt(assistant, owner.public_key().to_hex(), PLAINTEXT)
    tampered = payload[:-4] + ("AAAA" if not payload.endswith("AAAA") else "BBBB")

    with pytest.raises(nip44.Nip44Error) as failed:
        nip44.decrypt(owner, assistant.public_key().to_hex(), tampered)

    assert str(failed.value) == "did not decrypt"
    assert failed.value.__cause__ is None and failed.value.__suppress_context__
