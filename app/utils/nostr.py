from fastapi import HTTPException, status
from nostr_sdk import Keys, PublicKey


def generate_random_nsec() -> str:
    keys = Keys.generate()
    return keys.secret_key().to_bech32()


def to_hex_pubkey(value: str) -> str:
    """Hex or npub in, canonical hex out. Raises ValueError if unparseable.

    The pure half, so a pydantic ``field_validator`` can reuse it and still get
    422 aggregation — raising HTTPException inside a validator would bypass it.

    Does NOT strip: `resolve_pubkey_or_400` rejects padded input today and the
    other pubkey endpoints rely on that. Callers that want leniency strip first.
    """
    try:
        return PublicKey.parse(value).to_hex()
    except Exception:
        raise ValueError("must be a valid hex pubkey or npub")


def resolve_pubkey_or_400(value: str, param_name: str) -> str:
    """Hex or npub in, canonical hex out; anything unparseable is a 400."""
    try:
        return to_hex_pubkey(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{param_name} is not a valid hex pubkey or npub",
        )
