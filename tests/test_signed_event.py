import json

import pytest
from nostr_sdk import EventBuilder, Keys, Kind, Tag

from app.utils.signed_event import Rejection, SignedEventRejected, verify_signed_event

KEYS = Keys.generate()
OTHER = Keys.generate()
AUTHOR = KEYS.public_key().to_hex()


def _event(keys: Keys = KEYS, kind: int = 1) -> dict:
    event = (
        EventBuilder(Kind(kind), "hello")
        .tags([Tag.parse(["t", "x"])])
        .sign_with_keys(keys)
    )
    return json.loads(event.as_json())


def _flip(hex_str: str) -> str:
    return ("1" if hex_str[0] == "0" else "0") + hex_str[1:]


def _tamper(**changes) -> dict:
    return {**_event(), **changes}


def _with_duplicate_kind() -> str:
    return '{"kind": 2, ' + json.dumps(_event())[1:]


def test_valid_event_is_returned_verified():
    event = verify_signed_event(_event(), kind=1, author=AUTHOR)

    assert event.author().to_hex() == AUTHOR
    assert event.content() == "hello"


def test_author_is_optional():
    assert verify_signed_event(_event(), kind=1)


def test_json_string_is_accepted():
    assert verify_signed_event(json.dumps(_event()), kind=1, author=AUTHOR)


@pytest.mark.parametrize(
    "raw, kind, author, reason",
    [
        (None, 1, None, Rejection.MALFORMED),
        ("not json", 1, None, Rejection.MALFORMED),
        ("[" * 20000 + "]" * 20000, 1, None, Rejection.MALFORMED),
        (_with_duplicate_kind(), 1, None, Rejection.MALFORMED),
        (
            {k: v for k, v in _event().items() if k != "sig"},
            1,
            None,
            Rejection.MALFORMED,
        ),
        (_event(kind=2), 1, None, Rejection.WRONG_KIND),
        (_tamper(id=_flip(_event()["id"])), 1, None, Rejection.BAD_SIGNATURE),
        (_tamper(sig=_flip(_event()["sig"])), 1, None, Rejection.BAD_SIGNATURE),
        (_tamper(kind=2), 2, None, Rejection.BAD_SIGNATURE),
        (_tamper(content="bye"), 1, None, Rejection.BAD_SIGNATURE),
        (_tamper(tags=[["t", "y"]]), 1, None, Rejection.BAD_SIGNATURE),
        (_tamper(created_at=1), 1, None, Rejection.BAD_SIGNATURE),
        (
            _tamper(pubkey=OTHER.public_key().to_hex()),
            1,
            OTHER.public_key().to_hex(),
            Rejection.BAD_SIGNATURE,
        ),
        (_event(), 1, OTHER.public_key().to_hex(), Rejection.WRONG_AUTHOR),
        (_event(), 1, AUTHOR.upper(), Rejection.WRONG_AUTHOR),
    ],
    ids=[
        "not-str-or-dict",
        "not-json",
        "deeply-nested",
        "duplicate-key",
        "missing-sig",
        "wrong-kind",
        "forged-id",
        "forged-sig",
        "forged-kind",
        "forged-content",
        "forged-tags",
        "forged-created-at",
        "forged-author",
        "wrong-author",
        "author-not-normalised",
    ],
)
def test_rejections(raw, kind, author, reason):
    with pytest.raises(SignedEventRejected) as exc:
        verify_signed_event(raw, kind=kind, author=author)

    assert exc.value.reason is reason
