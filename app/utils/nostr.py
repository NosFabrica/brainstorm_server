import re

from fastapi import HTTPException, status
from nostr_sdk import Keys, PublicKey

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def generate_random_nsec() -> str:
    keys = Keys.generate()
    return keys.secret_key().to_bech32()


def to_hex_pubkey(value: str) -> str:
    """Hex or npub in, canonical hex out; ValueError if unparseable. Does not strip."""
    try:
        return PublicKey.parse(value).to_hex()
    except Exception:
        raise ValueError("must be a valid hex pubkey or npub")


def p_tag_pubkeys(tags: list) -> list[str]:
    """The `p` tags' pubkeys, lowercased, deduplicated, in order; anything that
    isn't 64-hex is dropped. An uppercase `p` would otherwise become its own graph
    node and get a TA under a `d` no client looks up."""
    seen: dict[str, None] = {}
    for tag in tags:
        if tag and tag[0] == "p" and len(tag) > 1 and isinstance(tag[1], str):
            pubkey = tag[1].lower()
            if _HEX64.match(pubkey):
                seen[pubkey] = None
    return list(seen)


def resolve_pubkey_or_400(value: str, param_name: str) -> str:
    """Hex or npub in, canonical hex out; anything unparseable is a 400."""
    try:
        return to_hex_pubkey(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{param_name} is not a valid hex pubkey or npub",
        )
