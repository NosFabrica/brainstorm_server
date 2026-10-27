"""The Observer's lightning provider key (NIP-57 `nostrPubkey`) rides on the calc
request with the 10040 keys, so the GrapeRank worker pins it at 95 too."""

import asyncio
import json

from app.services import zap_provider_service
from app.services.zap_provider_service import (
    decode_lnurl,
    lnurl_pay_url,
    parse_zap_provider_pubkey,
)

OBSERVER = "a" * 64
PROVIDER = "b" * 64


def _kind0(content: str) -> dict:
    return {"kind": 0, "pubkey": OBSERVER, "created_at": 1, "content": content}


# LUD-01's own example.
LUD01_LNURL = (
    "LNURL1DP68GURN8GHJ7UM9WFMXJCM99E3K7MF0V9CXJ0M385EKVCENXC6R2C35XVUKXEFCV5MKVV34"
    "X5EKZD3EV56NYD3HXQURZEPEXEJXXEPNXSCRVWFNV9NXZCN9XQ6XYEFHVGCXXCMYXYMNSERXFQ5FNS"
)
LUD01_URL = (
    "https://service.com/api?q="
    "3fc3645b439ce8e7f2553a69e5267081d96dcd340693afabe04be7b0ccd178df"
)


def test_lud16_maps_to_the_well_known_lnurlp_endpoint():
    assert (
        lnurl_pay_url({"lud16": "Alice@Getalby.com"})
        == "https://getalby.com/.well-known/lnurlp/alice"
    )


def test_lud16_accepts_punycode_domains():
    assert (
        lnurl_pay_url({"lud16": "bob@xn--e1afmkfd.xn--p1ai"})
        == "https://xn--e1afmkfd.xn--p1ai/.well-known/lnurlp/bob"
    )


def test_lud16_wins_over_lud06():
    assert lnurl_pay_url({"lud16": "alice@getalby.com", "lud06": LUD01_LNURL}) == (
        "https://getalby.com/.well-known/lnurlp/alice"
    )


def test_a_malformed_lud16_falls_back_to_lud06():
    assert lnurl_pay_url({"lud16": "not an address", "lud06": LUD01_LNURL}) == LUD01_URL


def test_no_lightning_address_means_no_endpoint():
    assert lnurl_pay_url({"name": "alice"}) is None
    assert lnurl_pay_url({"lud16": 42, "lud06": None}) is None


def test_decodes_a_bech32_lnurl():
    assert decode_lnurl(LUD01_LNURL) == LUD01_URL
    assert decode_lnurl("lightning:" + LUD01_LNURL.lower()) == LUD01_URL


def test_rejects_a_corrupted_lnurl():
    corrupted = LUD01_LNURL[:-1] + ("Q" if LUD01_LNURL[-1] != "Q" else "P")
    assert decode_lnurl(corrupted) is None
    assert decode_lnurl("npub1" + LUD01_LNURL[6:]) is None


def test_a_plain_http_lud06_is_not_fetched(monkeypatch):
    monkeypatch.setattr(
        zap_provider_service, "decode_lnurl", lambda s: "http://x.com/p"
    )
    assert lnurl_pay_url({"lud06": "lnurl1whatever"}) is None


def test_reads_nostr_pubkey_when_the_endpoint_allows_nostr():
    body = {"allowsNostr": True, "nostrPubkey": PROVIDER.upper(), "tag": "payRequest"}
    assert parse_zap_provider_pubkey(body, OBSERVER) == PROVIDER


def test_no_pubkey_without_allows_nostr():
    assert parse_zap_provider_pubkey({"nostrPubkey": PROVIDER}, OBSERVER) is None
    assert (
        parse_zap_provider_pubkey(
            {"allowsNostr": "true", "nostrPubkey": PROVIDER}, OBSERVER
        )
        is None
    )


def test_ignores_malformed_or_self_pubkeys():
    for pubkey in ["npub1xyz", "b" * 63, 7, OBSERVER]:
        assert (
            parse_zap_provider_pubkey(
                {"allowsNostr": True, "nostrPubkey": pubkey}, OBSERVER
            )
            is None
        )
    assert parse_zap_provider_pubkey(["allowsNostr"], OBSERVER) is None


def test_never_fetches_a_non_public_host():
    for url in [
        "http://getalby.com/.well-known/lnurlp/a",
        "https://127.0.0.1/.well-known/lnurlp/a",
        "https://localhost/.well-known/lnurlp/a",
        "https://10.0.0.5/.well-known/lnurlp/a",
        "https://user@getalby.com/.well-known/lnurlp/a",
    ]:
        assert asyncio.run(zap_provider_service._fetch_lnurl_pay(url)) is None, url


def _stub(monkeypatch, kind0, answer):
    asked = {}

    async def fake_profiles(pubkeys):
        asked["pubkeys"] = pubkeys
        return {} if kind0 is None else {OBSERVER: kind0}

    async def fake_pay(url):
        asked["url"] = url
        return answer

    monkeypatch.setattr(zap_provider_service, "fetch_profiles", fake_profiles)
    monkeypatch.setattr(zap_provider_service, "_fetch_lnurl_pay", fake_pay)
    return asked


def _lookup():
    return asyncio.run(zap_provider_service.fetch_zap_provider_pubkey(OBSERVER))


def test_fetches_the_provider_from_the_observers_kind0_on_the_search_relay(
    monkeypatch,
):
    kind0 = _kind0(json.dumps({"lud16": "alice@getalby.com"}))
    asked = _stub(monkeypatch, kind0, {"allowsNostr": True, "nostrPubkey": PROVIDER})
    assert _lookup() == PROVIDER
    assert asked == {
        "pubkeys": [OBSERVER],
        "url": "https://getalby.com/.well-known/lnurlp/alice",
    }


def test_no_kind0_or_no_address_means_no_provider(monkeypatch):
    _stub(monkeypatch, None, None)
    assert _lookup() is None
    _stub(monkeypatch, _kind0("not json"), None)
    assert _lookup() is None
    _stub(monkeypatch, _kind0(json.dumps({"name": "alice"})), None)
    assert _lookup() is None


def test_a_lookup_error_means_no_provider(monkeypatch):
    async def boom(*args, **kwargs):
        raise ConnectionError("down")

    monkeypatch.setattr(zap_provider_service, "fetch_profiles", boom)
    assert _lookup() is None
