"""WebAuthn assertion verification, for the passkey vault.

Only the slice of WebAuthn the vault needs: checking that a ``get()`` assertion
was made by a credential whose public key we already hold. There is no
registration ceremony to verify here — the public key arrives on an
authenticated PUT, from the owner of the key it protects (see
``app/routers/passkey_vault/CLAUDE.md``) — so no CBOR, no attestation.

Every check raises ``AssertionInvalid``; callers turn that into one uniform
answer, so the reason never reaches the wire.
"""

import base64
import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

# COSE algorithm ids a browser reports from getPublicKeyAlgorithm().
ALG_ES256 = -7
ALG_EDDSA = -8
ALG_RS256 = -257
SUPPORTED_ALGS = frozenset({ALG_ES256, ALG_EDDSA, ALG_RS256})

_FLAG_USER_PRESENT = 0x01
_FLAG_USER_VERIFIED = 0x04
_AUTH_DATA_MIN_LEN = 37


class AssertionInvalid(Exception):
    """The assertion does not prove what it claims. The message is for logs only."""


@dataclass(frozen=True)
class VerifiedAssertion:
    challenge: str
    sign_count: int


def b64url_decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise AssertionInvalid("not base64url") from error


def credential_id_hash(credential_id: str) -> str:
    """The vault's row key: sha256 of the raw credential id, hex."""
    return hashlib.sha256(b64url_decode(credential_id)).hexdigest()


def load_public_key(spki_b64url: str, alg: int):
    """The credential's public key, refusing one whose type doesn't match ``alg``."""
    try:
        key = serialization.load_der_public_key(b64url_decode(spki_b64url))
    except (ValueError, TypeError) as error:
        raise AssertionInvalid("public key is not SPKI DER") from error
    expected = {
        ALG_ES256: ec.EllipticCurvePublicKey,
        ALG_EDDSA: ed25519.Ed25519PublicKey,
        ALG_RS256: rsa.RSAPublicKey,
    }.get(alg)
    if expected is None or not isinstance(key, expected):
        raise AssertionInvalid("public key does not match its algorithm")
    if isinstance(key, ec.EllipticCurvePublicKey) and not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise AssertionInvalid("ES256 needs a P-256 key")
    return key


def _verify_signature(key, alg: int, signature: bytes, signed: bytes) -> None:
    try:
        if alg == ALG_ES256:
            key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        elif alg == ALG_EDDSA:
            key.verify(signature, signed)
        elif alg == ALG_RS256:
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise AssertionInvalid("unsupported algorithm")
    except InvalidSignature as error:
        raise AssertionInvalid("bad signature") from error


def verify_assertion(
    *,
    client_data_json: str,
    authenticator_data: str,
    signature: str,
    public_key: str,
    alg: int,
    allowed_origins: list[str],
) -> VerifiedAssertion:
    """Check a ``webauthn.get`` assertion against a stored public key.

    The challenge is returned, not checked: whether it was ours and is still
    unspent is the caller's question.

    The relying party is the origin's host — the UI creates every passkey with
    ``rp.id = location.hostname`` — so the origin decides which rpIdHash to
    expect, and only an allowed origin decides anything.
    """
    client_data_raw = b64url_decode(client_data_json)
    try:
        client_data = json.loads(client_data_raw)
    except ValueError as error:
        raise AssertionInvalid("clientDataJSON is not JSON") from error
    if not isinstance(client_data, dict):
        raise AssertionInvalid("clientDataJSON is not an object")
    if client_data.get("type") != "webauthn.get":
        raise AssertionInvalid("not a get() assertion")
    origin = client_data.get("origin")
    if origin not in allowed_origins:
        raise AssertionInvalid("origin not allowed")
    challenge = client_data.get("challenge")
    if not isinstance(challenge, str) or not challenge:
        raise AssertionInvalid("no challenge")

    auth_data = b64url_decode(authenticator_data)
    if len(auth_data) < _AUTH_DATA_MIN_LEN:
        raise AssertionInvalid("authenticatorData too short")
    rp_id = urlparse(origin).hostname or ""
    if auth_data[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise AssertionInvalid("rpIdHash mismatch")
    flags = auth_data[32]
    # The passkey stands in for a password, so it must have checked the person.
    if not flags & _FLAG_USER_PRESENT or not flags & _FLAG_USER_VERIFIED:
        raise AssertionInvalid("user not present and verified")

    signed = auth_data + hashlib.sha256(client_data_raw).digest()
    _verify_signature(
        load_public_key(public_key, alg), alg, b64url_decode(signature), signed
    )

    return VerifiedAssertion(
        challenge=challenge,
        sign_count=int.from_bytes(auth_data[33:37], "big"),
    )
