"""What a short code stands for: a `(kind, key)` entity and the on-site path it opens.

Pure helpers, no storage. Expected bech32 strings were produced once with
nostr_sdk and pinned here as literals.
"""

import pytest

from app.utils.shorturl_entities import build_entity_path, normalize_entity_key

PK_HEX = "f4d1866e8599563c52ceeedf11c28b8567e465c6e9a91df92add535d57f02ab0"
PK_NPUB = "npub17ngcvm59n9trc5kwam03rs5ts4n7gewxax53m7f2m4f464ls92cqr5qjta"


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------


def test_a_profile_key_is_hex_whether_given_hex_or_npub():
    assert normalize_entity_key("profile", PK_HEX) == PK_HEX
    assert normalize_entity_key("profile", PK_NPUB) == PK_HEX
    assert normalize_entity_key("profile", f"  {PK_HEX.upper()}  ") == PK_HEX


@pytest.mark.parametrize("bad", ["", "deadbeef", "z" * 64, "note1abc"])
def test_a_malformed_profile_key_is_rejected(bad):
    with pytest.raises(ValueError):
        normalize_entity_key("profile", bad)


def test_a_profile_without_relays_opens_its_npub():
    assert build_entity_path("profile", PK_HEX, []) == f"/p/{PK_NPUB}"


def test_a_profile_with_relays_opens_its_nprofile():
    assert build_entity_path("profile", PK_HEX, ["wss://nos.lol"]) == (
        "/p/nprofile1qqs0f5vxd6zej43u2t8wahc3c29c2elyvhrwn2galy4d656a2lcz4vqpp4mhxue69uhkummn9ekx7mqhx9ed4"
    )


# --------------------------------------------------------------------------
# event
# --------------------------------------------------------------------------

EVENT_ID = "5c83da77af1dec6d7289834998ad7aafbd9e2191396d75ec3cc27f5a77226f36"


def test_an_event_key_is_lowercase_hex():
    assert normalize_entity_key("event", f" {EVENT_ID.upper()} ") == EVENT_ID


@pytest.mark.parametrize("bad", ["", "deadbeef", "z" * 64, "note1abc", PK_NPUB])
def test_a_malformed_event_key_is_rejected(bad):
    with pytest.raises(ValueError):
        normalize_entity_key("event", bad)


def test_an_event_opens_its_nevent_without_relays():
    assert build_entity_path("event", EVENT_ID, []) == (
        "/e/nevent1qqs9eq76w7h3mmrdw2ycxjvc44a2l0v7yxgnjmt4as7vyl66wu3x7dsrtwlqa"
    )


def test_an_event_nevent_carries_its_stored_relays():
    assert build_entity_path("event", EVENT_ID, ["wss://nos.lol"]) == (
        "/e/nevent1qqs9eq76w7h3mmrdw2ycxjvc44a2l0v7yxgnjmt4as7vyl66wu3x7dspp4mhxue69uhkummn9ekx7mqpdj05k"
    )


# --------------------------------------------------------------------------
# address
# --------------------------------------------------------------------------


def test_an_address_splits_on_the_first_two_colons_only():
    key = f"30023:{PK_HEX.upper()}:my:art"
    assert normalize_entity_key("address", key) == f"30023:{PK_HEX}:my:art"


def test_an_address_may_have_an_empty_d():
    assert normalize_entity_key("address", f"30023:{PK_HEX}:") == f"30023:{PK_HEX}:"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        f"30023:{PK_HEX}",
        f"x:{PK_HEX}:d",
        f"-1:{PK_HEX}:d",
        f"65536:{PK_HEX}:d",
        "30023:deadbeef:d",
        f"30023:{PK_NPUB}:d",
        f"30023:{PK_HEX}:" + "d" * 600,
    ],
)
def test_a_malformed_address_key_is_rejected(bad):
    with pytest.raises(ValueError):
        normalize_entity_key("address", bad)


def test_an_address_opens_its_naddr():
    assert build_entity_path("address", f"30023:{PK_HEX}:my:art", []) == (
        "/e/naddr1qqrx67f6v9e8gq3q7ngcvm59n9trc5kwam03rs5ts4n7gewxax53m7f2m4f464ls92cqxpqqqp65wyqf58t"
    )


def test_an_address_naddr_carries_its_stored_relays():
    assert build_entity_path("address", f"30023:{PK_HEX}:", ["wss://nos.lol"]) == (
        "/e/naddr1qqqqyg856xrxapve2c799nhwmugu9zu9vljxt3hf4ywlj2ka2dw40up2kqpsgqqqw4rszrthwden5te0dehhxtnvdakqeucz6d"
    )


@pytest.mark.parametrize(
    "kind,expected",
    [
        (
            39999,
            "/e/naddr1qqqhsq3q7ngcvm59n9trc5kwam03rs5ts4n7gewxax53m7f2m4f464ls92cqxpqqqzwr7qguwaehxw309ajxxmmndshxyunpd9h8xar0wfkjuam0wfkxgd0n6r7",
        ),
        (
            39998,
            "/e/naddr1qqqhsq3q7ngcvm59n9trc5kwam03rs5ts4n7gewxax53m7f2m4f464ls92cqxpqqqzwruqguwaehxw309ajxxmmndshxyunpd9h8xar0wfkjuam0wfkxgrcvruu",
        ),
    ],
)
def test_a_dlist_address_without_relays_points_at_dcosl(kind, expected):
    assert build_entity_path("address", f"{kind}:{PK_HEX}:x", []) == expected


def test_a_dlist_address_with_stored_relays_keeps_them_instead_of_dcosl():
    assert build_entity_path("address", f"39999:{PK_HEX}:x", ["wss://nos.lol"]) == (
        "/e/naddr1qqqhsq3q7ngcvm59n9trc5kwam03rs5ts4n7gewxax53m7f2m4f464ls92cqxpqqqzwr7qgdwaehxw309ahx7uewd3hkchepktk"
    )


# --------------------------------------------------------------------------
# hashtag
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,stored",
    [
        ("bitcoin", "bitcoin"),
        ("  #Bitcoin ", "bitcoin"),
        ("##nostr", "#nostr"),
        ("Café", "café"),
    ],
)
def test_a_hashtag_is_trimmed_lowercased_and_loses_one_leading_hash(given, stored):
    assert normalize_entity_key("hashtag", given) == stored


@pytest.mark.parametrize(
    "bad", ["", "  ", "#", "a/b", "two words", "tab\there", "x" * 65]
)
def test_a_malformed_hashtag_is_rejected(bad):
    with pytest.raises(ValueError):
        normalize_entity_key("hashtag", bad)


def test_a_64_char_hashtag_is_accepted():
    assert normalize_entity_key("hashtag", "#" + "x" * 64) == "x" * 64


@pytest.mark.parametrize(
    "word,path",
    [("bitcoin", "/t/bitcoin"), ("café", "/t/caf%C3%A9"), ("#c++", "/t/%23c%2B%2B")],
)
def test_a_hashtag_opens_its_uri_encoded_t_page(word, path):
    assert build_entity_path("hashtag", word, []) == path


# --------------------------------------------------------------------------
# tag page
# --------------------------------------------------------------------------


def test_a_tag_key_splits_on_the_first_colon_and_lowercases_the_author():
    key = f"{PK_HEX.upper()}:Some:Slug"
    assert normalize_entity_key("tag", key) == f"{PK_HEX}:Some:Slug"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        PK_HEX,
        f"{PK_HEX}:",
        f"{PK_NPUB}:slug",
        "deadbeef:slug",
        f"{PK_HEX}:" + "s" * 600,
    ],
)
def test_a_malformed_tag_key_is_rejected(bad):
    with pytest.raises(ValueError):
        normalize_entity_key("tag", bad)


def test_a_tag_opens_its_author_npub_and_encoded_slug():
    assert (
        build_entity_path("tag", f"{PK_HEX}:a b/c", []) == f"/tags/{PK_NPUB}/a%20b%2Fc"
    )


# --------------------------------------------------------------------------
# kinds
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["", "url", "Profile", "note"])
def test_an_unknown_kind_is_rejected(kind):
    with pytest.raises(ValueError):
        normalize_entity_key(kind, PK_HEX)


def test_a_stored_relay_nostr_sdk_cannot_encode_is_left_out_of_the_path():
    """Rows minted under the older, looser relay check must still resolve."""
    assert build_entity_path("profile", PK_HEX, ["wss://a b"]) == f"/p/{PK_NPUB}"


@pytest.mark.parametrize(
    "search", ["wss://search.brainstorm.world", "WSS://search.brainstorm.world/"]
)
def test_a_stored_search_relay_is_left_out_of_the_path(search):
    """Legacy rows may hold it; it is never a hint."""
    assert build_entity_path("profile", PK_HEX, [search]) == f"/p/{PK_NPUB}"
    assert build_entity_path("address", f"39999:{PK_HEX}:x", [search]) == (
        build_entity_path("address", f"39999:{PK_HEX}:x", [])
    )
