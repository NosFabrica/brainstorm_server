"""End-to-end tests for GET /.well-known/nostr.json (NIP-05 verification).

Real HTTP through ASGI against a minimal app; only the DB boundary is stubbed.
The expected local-parts come from the production derivation function, so these
assert the route agrees with whatever the kind-0 publisher advertises.
"""

from __future__ import annotations

from app.utils.assistant_nip05 import compute_assistant_nip05_local_part

ASSISTANT_PUBKEY = "a" * 64
OTHER_ASSISTANT_PUBKEY = "b" * 64
HOUSE_PUBKEY = "c" * 64

WELL_KNOWN_PATH = "/.well-known/nostr.json"


class TestNameResolution:
    def test_known_name_resolves_to_its_pubkey(self, make_client):
        client = make_client(
            assistants=(OTHER_ASSISTANT_PUBKEY, ASSISTANT_PUBKEY),
        )
        name = compute_assistant_nip05_local_part(ASSISTANT_PUBKEY)

        r = client.get(WELL_KNOWN_PATH, params={"name": name})

        assert r.status_code == 200
        assert r.json() == {"names": {name: ASSISTANT_PUBKEY}}

    def test_hit_carries_the_cors_header_and_json_content_type(self, make_client):
        client = make_client(assistants=(ASSISTANT_PUBKEY,))
        name = compute_assistant_nip05_local_part(ASSISTANT_PUBKEY)

        r = client.get(WELL_KNOWN_PATH, params={"name": name})

        assert r.headers.get("access-control-allow-origin") == "*"
        assert r.headers["content-type"].startswith("application/json")

    def test_unknown_name_returns_empty_names(self, make_client):
        client = make_client(assistants=(ASSISTANT_PUBKEY,))

        r = client.get(WELL_KNOWN_PATH, params={"name": "nobody_here_0000"})

        assert r.status_code == 200
        assert r.json() == {"names": {}}

    def test_missing_name_never_dumps_the_assistant_list(
        self, make_client, assistant_pubkeys_query
    ):
        client = make_client(assistants=(ASSISTANT_PUBKEY, OTHER_ASSISTANT_PUBKEY))

        r = client.get(WELL_KNOWN_PATH)

        assert r.status_code == 200
        assert r.json() == {"names": {}}
        # Not merely filtered out of the response — never queried at all.
        assistant_pubkeys_query.assert_not_awaited()


class TestHouseIdentity:
    def test_underscore_resolves_to_the_periodic_graperank_pubkey(self, make_client):
        client = make_client(house_pubkey=HOUSE_PUBKEY)

        r = client.get(WELL_KNOWN_PATH, params={"name": "_"})

        assert r.status_code == 200
        assert r.json() == {"names": {"_": HOUSE_PUBKEY}}

    def test_underscore_is_absent_when_no_house_identity_configured(self, make_client):
        client = make_client(house_pubkey="")

        r = client.get(WELL_KNOWN_PATH, params={"name": "_"})

        assert r.status_code == 200
        assert r.json() == {"names": {}}


class TestCorsHeader:
    def test_header_present_without_an_origin_header(self, make_client):
        client = make_client(house_pubkey=HOUSE_PUBKEY)

        r = client.get(WELL_KNOWN_PATH, params={"name": "_"})

        assert r.headers.get("access-control-allow-origin") == "*"

    def test_header_present_on_a_miss_from_a_browser_client(self, make_client):
        client = make_client(assistants=(ASSISTANT_PUBKEY,))

        r = client.get(
            WELL_KNOWN_PATH,
            params={"name": "nobody_here_0000"},
            headers={"Origin": "https://nostr.example"},
        )

        assert r.headers.get("access-control-allow-origin") == "*"


class TestProductionAggregator:
    def test_real_app_serves_the_route(self, client):
        """Pins the wiring on `app.api:app` itself, not on a minimal test app.
        The no-name path answers without touching the DB, so no stub is needed.
        """
        r = client.get(WELL_KNOWN_PATH)

        assert r.status_code == 200
        assert r.json() == {"names": {}}
        assert r.headers.get("access-control-allow-origin") == "*"
