"""Short-code format and relay-set fingerprinting — the pure half of the shortener.

Codes are Crockford base32: uppercase, and never `I`, `L`, `O` or `U`. Those four
are the glyphs people mistype when a link is read aloud or retyped, so resolution
folds them back (`I`/`L` -> `1`, `O` -> `0`) and is case-insensitive.

Issue: .scratch/shorturl/issues/02-durable-storage.md
"""

import pytest

from app.services.shorturl_service import (
    SHORT_CODE_LENGTH,
    generate_short_code,
    normalize_short_code,
    relays_fingerprint,
)

_AMBIGUOUS = set("ILOU")


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------


def test_codes_are_the_configured_length():
    assert len(generate_short_code()) == SHORT_CODE_LENGTH


def test_codes_are_uppercase_crockford_base32():
    alphabet = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    for _ in range(200):
        code = generate_short_code()
        assert set(code) <= alphabet, code


def test_codes_never_contain_a_confusable_glyph():
    """I/L/O/U are excluded so a retyped code can be folded unambiguously."""
    for _ in range(200):
        assert not (set(generate_short_code()) & _AMBIGUOUS)


def test_codes_are_not_obviously_repeating():
    codes = {generate_short_code() for _ in range(200)}
    assert len(codes) == 200


# --------------------------------------------------------------------------
# normalization — how a typed or scanned code is resolved
# --------------------------------------------------------------------------


def test_lowercase_resolves_to_the_stored_form():
    assert normalize_short_code("ab3xk9qz") == "AB3XK9QZ"


def test_uppercase_is_unchanged():
    assert normalize_short_code("AB3XK9QZ") == "AB3XK9QZ"


def test_mixed_case_resolves():
    assert normalize_short_code("aB3xK9Qz") == "AB3XK9QZ"


@pytest.mark.parametrize(
    "typed,stored",
    [
        ("I23", "123"),
        ("i23", "123"),
        ("L23", "123"),
        ("l23", "123"),
        ("O12", "012"),
        ("o12", "012"),
        ("IOL", "101"),
    ],
)
def test_confusable_glyphs_fold_to_their_digits(typed, stored):
    assert normalize_short_code(typed) == stored


def test_surrounding_whitespace_is_ignored():
    assert normalize_short_code("  ab3xk9qz  ") == "AB3XK9QZ"


def test_a_generated_code_normalizes_to_itself():
    """Folding must never alter a code we minted, or lookups would miss."""
    for _ in range(200):
        code = generate_short_code()
        assert normalize_short_code(code) == code


def test_u_is_not_folded():
    """Crockford folds I/L/O only; U is merely excluded from the alphabet."""
    assert normalize_short_code("U") == "U"


# --------------------------------------------------------------------------
# relay-set fingerprint — what makes minting idempotent
# --------------------------------------------------------------------------


def test_the_same_set_fingerprints_the_same():
    relays = ["wss://relay.damus.io", "wss://nos.lol"]
    assert relays_fingerprint(relays) == relays_fingerprint(relays)


def test_order_does_not_matter():
    a = relays_fingerprint(["wss://a.example", "wss://b.example"])
    b = relays_fingerprint(["wss://b.example", "wss://a.example"])
    assert a == b


def test_duplicates_do_not_matter():
    a = relays_fingerprint(["wss://a.example"])
    b = relays_fingerprint(["wss://a.example", "wss://a.example"])
    assert a == b


def test_case_and_trailing_slash_do_not_matter():
    a = relays_fingerprint(["wss://A.example/", "wss://b.example"])
    b = relays_fingerprint(["wss://a.example", "wss://B.EXAMPLE/"])
    assert a == b


def test_an_empty_set_has_its_own_stable_fingerprint():
    assert relays_fingerprint([]) == relays_fingerprint([])
    assert relays_fingerprint([]) != relays_fingerprint(["wss://a.example"])


def test_different_sets_differ():
    a = relays_fingerprint(["wss://a.example"])
    b = relays_fingerprint(["wss://a.example", "wss://b.example"])
    assert a != b
