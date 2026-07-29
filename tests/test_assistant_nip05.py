"""The deterministic Assistant NIP-05 derivation (pure function, no I/O)."""

import random
import re

import pytest

from app.core.config import settings
from app.utils.assistant_nip05 import (
    BIP39_ENGLISH_WORDS,
    compute_assistant_nip05,
    compute_assistant_nip05_local_part,
)

LOCAL_PART_RE = re.compile(r"^[a-z]+_[a-z]+_[0-9a-f]{4}$")

# Frozen pairs: drift here would invalidate already-published kind 0 events.
PINNED_LOCAL_PARTS = [
    (
        "be7bf5de068c1d842ed34a7c270507ec940f5ea51671cfd062a95e9d09420d0a",
        "museum_travel_16e2",
    ),
    ("00" * 32, "snack_fiber_f862"),
    ("ff" * 32, "version_human_0f72"),
    (
        "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d",
        "bar_expose_8660",
    ),
    (
        "6e468422dfb74a5738702a8823b9b28168abab8655faacb6853cd0ee15deee93",
        "fossil_color_9b56",
    ),
]

SAMPLE_PUBKEYS = [pubkey for pubkey, _expected in PINNED_LOCAL_PARTS] + [
    "82341f882b6eabcd2ba7f1ef90aad961cf074af15b9ef44a09f9d2a8fbfbe6a2",
    "a" * 64,
]


def arbitrary_pubkeys(count: int) -> list[str]:
    """Seeded, so a format failure is reproducible rather than flaky."""
    rng = random.Random(20260729)
    return [rng.randbytes(32).hex() for _ in range(count)]


@pytest.mark.parametrize("pubkey", SAMPLE_PUBKEYS)
def test_local_part_is_deterministic(pubkey: str) -> None:
    first = compute_assistant_nip05_local_part(pubkey)
    assert first == compute_assistant_nip05_local_part(pubkey)
    assert first == compute_assistant_nip05_local_part(pubkey)


@pytest.mark.parametrize("pubkey", SAMPLE_PUBKEYS)
def test_local_part_matches_format(pubkey: str) -> None:
    assert LOCAL_PART_RE.match(compute_assistant_nip05_local_part(pubkey))


def test_local_part_matches_format_for_arbitrary_pubkeys() -> None:
    for pubkey in arbitrary_pubkeys(1000):
        local_part = compute_assistant_nip05_local_part(pubkey)
        assert LOCAL_PART_RE.match(local_part), f"{pubkey} -> {local_part}"


@pytest.mark.parametrize("pubkey,expected", PINNED_LOCAL_PARTS)
def test_local_part_pinned_regressions(pubkey: str, expected: str) -> None:
    assert compute_assistant_nip05_local_part(pubkey) == expected


def test_distinct_pubkeys_yield_distinct_local_parts() -> None:
    local_parts = {compute_assistant_nip05_local_part(pk) for pk in SAMPLE_PUBKEYS}
    assert len(local_parts) == len(SAMPLE_PUBKEYS)


def test_words_come_from_the_bip39_wordlist() -> None:
    assert len(BIP39_ENGLISH_WORDS) == 2048
    for pubkey in SAMPLE_PUBKEYS:
        word1, word2, _hex4 = compute_assistant_nip05_local_part(pubkey).split("_")
        assert word1 in BIP39_ENGLISH_WORDS
        assert word2 in BIP39_ENGLISH_WORDS


def test_invalid_pubkey_hex_raises() -> None:
    with pytest.raises(ValueError):
        compute_assistant_nip05_local_part("not-hex")


@pytest.mark.parametrize(
    "frontend_url,expected_domain",
    [
        ("https://brainstorm.world", "brainstorm.world"),
        (
            "https://brainstorm-staging.nosfabrica.com/",
            "brainstorm-staging.nosfabrica.com",
        ),
        ("http://localhost:3000", "localhost"),
        ("https://BRAINSTORM.WORLD", "brainstorm.world"),
    ],
)
def test_full_identifier_uses_frontend_url_hostname(
    monkeypatch: pytest.MonkeyPatch, frontend_url: str, expected_domain: str
) -> None:
    monkeypatch.setattr(settings, "frontend_url", frontend_url)
    pubkey = SAMPLE_PUBKEYS[0]
    expected_local = compute_assistant_nip05_local_part(pubkey)
    assert compute_assistant_nip05(pubkey) == f"{expected_local}@{expected_domain}"


@pytest.mark.parametrize("frontend_url", ["", "   ", "brainstorm.world", "not a url"])
def test_full_identifier_is_none_without_a_hostname(
    monkeypatch: pytest.MonkeyPatch, frontend_url: str
) -> None:
    monkeypatch.setattr(settings, "frontend_url", frontend_url)
    assert compute_assistant_nip05(SAMPLE_PUBKEYS[0]) is None
