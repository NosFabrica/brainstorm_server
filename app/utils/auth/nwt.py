"""ORE-A: Nostr Web Tokens (NWT) verification.

NWT is a signed Nostr event of kind 27519 conveying signed claims from a client
to an Open Ranking provider. Claims are carried as event tags:

    ["aud",  "<provider-domain>"]   required when the provider enforces audience
    ["iat",  "<unix-seconds>"]      issued-at, optional but sanity-checked
    ["nbf",  "<unix-seconds>"]      not-before, optional
    ["exp",  "<unix-seconds>"]      expiry, optional but strongly recommended

Transport per ORE-A: `Authorization: Nostr <base64url-no-padding-token>` where
the token is the JSON-serialized signed event.

Reference: https://github.com/Open-Ranking/nostr-web-tokens

This validator is independent of the existing NIP-98 (kind 27235) validator;
it deliberately does NOT check `method`, `u`, or `payload` tags.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status
from nostr_sdk import Event

from app.core.config import settings

NWT_KIND = 27519

# Recommended clock skew per the NWT spec ("Verifiers SHOULD allow a small
# clock skew, e.g. ±60 seconds"). Applied symmetrically to nbf / exp and as
# the forward bound on iat / created_at.
NWT_CLOCK_SKEW_SECONDS = 60


def _expected_audience() -> str:
    """The audience this provider identifies as for the purposes of the NWT
    `aud` claim. Derived from `settings.public_base_url` so it stays in sync
    with the hostname clients reach us at — no separate env var needed.
    """
    parsed = urlparse(settings.public_base_url)
    # `netloc` returns "host:port"; the spec is hostname-oriented so strip
    # the port. Falls back to the raw value if the URL is unparseable.
    if parsed.hostname:
        return parsed.hostname
    return settings.public_base_url


def _b64url_decode_no_pad(value: str) -> bytes:
    """Decode an unpadded base64url string. NWT mandates no padding."""
    s = value.strip()
    # `base64.urlsafe_b64decode` requires correct padding; pad to a multiple of 4.
    padding = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * padding))


def _find_tag_value(event: Event, name: str) -> Optional[str]:
    """Return the FIRST value for the named tag, or None."""
    for tag in event.tags().to_vec():
        vec = tag.as_vec()
        if len(vec) >= 2 and vec[0] == name:
            return vec[1]
    return None


def _find_tag_values(event: Event, name: str) -> list[str]:
    """Return ALL values for the named tag. The NWT spec allows a token to
    carry multiple `aud` tags (one per intended recipient); a verifier
    accepts the token when ANY of them matches.
    """
    out: list[str] = []
    for tag in event.tags().to_vec():
        vec = tag.as_vec()
        if len(vec) >= 2 and vec[0] == name:
            out.append(vec[1])
    return out


def _unauthorized(detail: str) -> HTTPException:
    # ORE-A defers HTTP error codes to the NWT spec. 401 is the canonical
    # "invalid or missing credentials" choice; X-Reason carries the human-
    # readable reason and is attached by the FastAPI exception handler.
    exc = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
    exc.headers = {"X-Reason": detail, "WWW-Authenticate": "Nostr"}
    return exc


def _bad_token(detail: str) -> HTTPException:
    exc = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    exc.headers = {"X-Reason": detail, "WWW-Authenticate": "Nostr"}
    return exc


def _parse_unix_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_nwt_token(encoded_token: str) -> str:
    """Validate the value of an `Authorization: Nostr <token>` header.

    Returns the signer's pubkey (hex) on success. Raises HTTPException with
    `X-Reason` on any failure.
    """
    try:
        decoded = _b64url_decode_no_pad(encoded_token).decode("utf-8")
    except Exception:
        raise _unauthorized("Bad NWT: invalid base64url")

    try:
        event = Event.from_json(decoded)
    except Exception:
        raise _unauthorized("Bad NWT: invalid event JSON")

    if event.kind().as_u16() != NWT_KIND:
        raise _unauthorized(f"Bad NWT: expected kind {NWT_KIND}")

    if not event.verify_signature():
        raise _unauthorized("Bad NWT: invalid signature")

    now = int(datetime.now(tz=timezone.utc).timestamp())

    # Issued-at sanity. Per spec, when `iat` is present verifiers MUST use it
    # in place of `created_at`. If neither is acceptable as a "not too far in
    # the future" value, the token is rejected — defends against tokens whose
    # validity window is shifted forward by clock manipulation.
    iat_tag = _parse_unix_int(_find_tag_value(event, "iat"))
    issued_at = iat_tag if iat_tag is not None else event.created_at().as_secs()
    if issued_at - now > NWT_CLOCK_SKEW_SECONDS:
        raise _bad_token("Bad NWT: iat is in the future")

    # nbf: token not yet valid. Allow clock skew so a clock-drifted client
    # whose nbf is a few seconds ahead of the server still goes through.
    nbf = _parse_unix_int(_find_tag_value(event, "nbf"))
    if nbf is not None and now + NWT_CLOCK_SKEW_SECONDS < nbf:
        raise _bad_token("Bad NWT: token not yet valid (nbf)")

    # exp: token expired. Same symmetric skew.
    exp = _parse_unix_int(_find_tag_value(event, "exp"))
    if exp is not None and now - NWT_CLOCK_SKEW_SECONDS >= exp:
        raise _bad_token("Bad NWT: token expired (exp)")

    # Audience check. The NWT spec allows MULTIPLE `aud` tags on a single
    # token — accept the token when ANY of them matches this provider. The
    # expected audience is the hostname of the provider's public_base_url.
    expected_aud = _expected_audience()
    aud_values = _find_tag_values(event, "aud")
    if not aud_values:
        raise _bad_token("Bad NWT: missing aud claim")
    if expected_aud not in aud_values:
        raise _bad_token(f"Bad NWT: aud {aud_values} does not include this provider")

    return event.author().to_hex()


def _extract_nwt_from_header(request: Request) -> Optional[str]:
    """Pull the NWT token out of `Authorization: Nostr <token>`. Returns
    None if the header is absent or uses a different scheme.
    """
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.strip().partition(" ")
    if scheme.lower() != "nostr" or not token:
        return None
    return token.strip()


async def optional_nwt_signer(request: Request) -> Optional[str]:
    """FastAPI dependency honouring `settings.open_ranking_require_auth`.

    - Auth enforced (flag True): REQUIRE a valid NWT and return the signer's
      pubkey. Handlers force the observer perspective to this pubkey, so a
      caller can only ever query their own scores.
    - Open mode (flag False): return None. No NWT is required; handlers fall
      back to the public global observer plus any client-supplied `pov`.

    Returning the signer (or None) lets each handler decide how to resolve the
    observer without re-reading the header.
    """
    if not settings.open_ranking_require_auth:
        return None
    token = _extract_nwt_from_header(request)
    if not token:
        raise _unauthorized("Missing Authorization: Nostr <token> header")
    return validate_nwt_token(token)
