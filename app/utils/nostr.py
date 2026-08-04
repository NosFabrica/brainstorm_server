from fastapi import HTTPException, status
from nostr_sdk import Keys, PublicKey


def generate_random_nsec() -> str:
    keys = Keys.generate()
    return keys.secret_key().to_bech32()


def resolve_pubkey_or_400(value: str, param_name: str) -> str:
    """Hex or npub in, canonical hex out; anything unparseable is a 400."""
    try:
        return PublicKey.parse(value).to_hex()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{param_name} is not a valid hex pubkey or npub",
        )
