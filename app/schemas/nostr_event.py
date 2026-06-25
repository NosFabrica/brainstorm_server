"""Pydantic model of a signed Nostr event (NIP-01 envelope).

Typing request bodies as :class:`NostrEvent` instead of a bare ``dict`` gives us
two things the opaque ``dict`` could not: the real event shape shows up in the
OpenAPI/Swagger docs, and structurally-malformed input is rejected with a
field-level 422 *before* it reaches ``nostr_sdk`` (which would otherwise raise an
unhandled error and surface as a 500).

This is the generic NIP-01 envelope plus the one NIP-02 rule that bites the
follow-list path: a ``p`` tag must carry a pubkey. Cryptographic checks (matching
author, valid signature) stay in the service layer where they map to 401/403.
"""

import re

from pydantic import BaseModel, Field, field_validator

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_HEX_128 = re.compile(r"^[0-9a-f]{128}$")


class NostrEvent(BaseModel):
    """A signed Nostr event in its canonical NIP-01 wire form.

    Field names are the canonical lowercase NIP-01 keys (``created_at``, not
    ``createdAt``) — this is an external wire spec, so it is intentionally *not*
    camelCased like the rest of the public API.
    """

    id: str = Field(description="32-byte event id, lowercase hex (64 chars).")
    pubkey: str = Field(description="Author public key, lowercase hex (64 chars).")
    created_at: int = Field(ge=0, description="Unix timestamp in seconds.")
    kind: int = Field(ge=0, description="Event kind (3 for a NIP-02 follow list).")
    tags: list[list[str]] = Field(
        description="NIP-01 tags; each is a non-empty list of strings."
    )
    content: str = Field(description="Event content (empty for a follow list).")
    sig: str = Field(description="Schnorr signature, lowercase hex (128 chars).")

    @field_validator("id", "pubkey")
    @classmethod
    def _is_hex_64(cls, value: str) -> str:
        if not _HEX_64.fullmatch(value):
            raise ValueError("must be 64 lowercase hex characters")
        return value

    @field_validator("sig")
    @classmethod
    def _is_hex_128(cls, value: str) -> str:
        if not _HEX_128.fullmatch(value):
            raise ValueError("must be 128 lowercase hex characters")
        return value

    @field_validator("tags")
    @classmethod
    def _tags_well_formed(cls, tags: list[list[str]]) -> list[list[str]]:
        for tag in tags:
            if not tag:
                raise ValueError("tags must not be empty")
            # NIP-02: a "p" (follow) tag must carry a pubkey as its value.
            if tag[0] == "p":
                if len(tag) < 2 or not _HEX_64.fullmatch(tag[1]):
                    raise ValueError(
                        "a 'p' tag must carry a 64-hex pubkey as its value"
                    )
        return tags
