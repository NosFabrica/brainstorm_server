"""The one way to accept a signed Nostr event from outside."""

import json
from enum import StrEnum
from typing import Any

from nostr_sdk import Event


class Rejection(StrEnum):
    MALFORMED = "malformed"
    WRONG_KIND = "wrong_kind"
    BAD_SIGNATURE = "bad_signature"
    WRONG_AUTHOR = "wrong_author"


class SignedEventRejected(Exception):
    def __init__(self, reason: Rejection):
        super().__init__(reason.value)
        self.reason = reason


def verify_signed_event(raw: Any, *, kind: int, author: str | None = None) -> Event:
    """Parse, check kind, verify id + sig, check author. Read tags from the result.

    Pass JSON as received when there is one: nostr_sdk rejects duplicate keys.
    """
    if not isinstance(raw, (str, dict)):
        raise SignedEventRejected(Rejection.MALFORMED)
    try:
        event = Event.from_json(raw if isinstance(raw, str) else json.dumps(raw))
    except Exception:
        raise SignedEventRejected(Rejection.MALFORMED)
    if event.kind().as_u16() != kind:
        raise SignedEventRejected(Rejection.WRONG_KIND)
    if not event.verify():
        raise SignedEventRejected(Rejection.BAD_SIGNATURE)
    if author is not None and event.author().to_hex() != author:
        raise SignedEventRejected(Rejection.WRONG_AUTHOR)
    return event
