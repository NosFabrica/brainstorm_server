import base64
import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException, Request, status
from nostr_sdk import Event

from app.core.config import settings

NIP98_KIND = 27235
NIP98_MAX_CLOCK_SKEW_SECONDS = 60


def _expected_request_url(request: Request) -> str:
    base = settings.public_base_url.rstrip("/")
    path = request.url.path
    query = request.url.query
    return f"{base}{path}?{query}" if query else f"{base}{path}"


def _find_tag_value(event: Event, name: str) -> str | None:
    for tag in event.tags().to_vec():
        vec = tag.as_vec()
        if len(vec) >= 2 and vec[0] == name:
            return vec[1]
    return None


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


async def validate_nip98_event(request: Request, encoded_event: str) -> str:
    """Validate the value of an `Authorization: Nostr <encoded_event>` header.

    Returns the signer pubkey (hex) on success. Raises 401 on any failure.
    """
    try:
        decoded = base64.b64decode(encoded_event.strip(), validate=True).decode("utf-8")
    except Exception:
        raise _unauthorized("Bad NIP-98 token: invalid base64")

    try:
        event = Event.from_json(decoded)
    except Exception:
        raise _unauthorized("Bad NIP-98 token: invalid event JSON")

    if event.kind().as_u16() != NIP98_KIND:
        raise _unauthorized(f"Bad NIP-98 token: expected kind {NIP98_KIND}")

    if not event.verify_signature():
        raise _unauthorized("Bad NIP-98 token: invalid signature")

    created_at = event.created_at().as_secs()
    now = int(datetime.now(tz=timezone.utc).timestamp())
    if abs(now - created_at) > NIP98_MAX_CLOCK_SKEW_SECONDS:
        raise _unauthorized("Bad NIP-98 token: created_at outside accepted window")

    method_tag = _find_tag_value(event, "method")
    if not method_tag or method_tag.upper() != request.method.upper():
        raise _unauthorized("Bad NIP-98 token: HTTP method mismatch")

    url_tag = _find_tag_value(event, "u")
    if not url_tag or url_tag != _expected_request_url(request):
        raise _unauthorized("Bad NIP-98 token: URL mismatch")

    # Per NIP-98: when the request has a body, the event MUST carry a `payload`
    # tag containing the lowercase hex sha256 of the raw body bytes. Reading the
    # body here is safe — Starlette caches it on the Request, so downstream
    # body-parsing in the route handler reuses the cached bytes.
    body = await request.body()
    payload_tag = _find_tag_value(event, "payload")
    if body:
        if not payload_tag:
            raise _unauthorized("Bad NIP-98 token: missing payload tag for request with body")
        expected = hashlib.sha256(body).hexdigest()
        if payload_tag.lower() != expected:
            raise _unauthorized("Bad NIP-98 token: payload hash mismatch")

    return event.author().to_hex()
