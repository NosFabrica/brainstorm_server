"""The Assistant's Duty Menu: `GET` and `POST /user/assistantDutyMenu`.

Brainstorm-UI shows the menu as current only when the published `duty` rows,
as a set of (key, relay), equal the GET's `rows` — so the assertions here are
on exact shape, as `test_setup_designation.py`'s are. The relays are faked at
the service's import site; signing is real, with a generated Assistant key.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient
from nostr_sdk import Client, Event, EventBuilder, Keys, Kind, Tag, Timestamp

from app.api import app
from app.core.config import settings
from app.core.database import get_db
from app.routers.setup import router as setup_router
from app.services.duty_menu_build import (
    DUTY_MENU_HEADER,
    NOUS_MENU_RELAYS,
    build_menu_tags,
    is_duty_menu,
    menu_created_at,
    publish_targets,
    relay_key,
    write_url,
)

PUBLIC_TA = "wss://scores.example"
INTERNAL_TA = "ws://strfry.internal:7777"
# Apart from Nous' relay, so a check of one can't pass on the other.
DICTIONARY = "wss://dictionary.example"
NOUS = NOUS_MENU_RELAYS[0]
OUTBOX = ["wss://relay.damus.io", "wss://relay.primal.net"]

EXPECTED_KEYS = [
    "30382:rank",
    "30382:followers",
    "30382:reporters",
    "30382:muters",
    "30382:hops",
    "30392",
    "39998",
    "39999",
]


@pytest.fixture
def assistant() -> Keys:
    return Keys.generate()


@pytest.fixture(autouse=True)
def relay_settings(monkeypatch):
    monkeypatch.setattr(settings, "nostr_upload_ta_events_relay_public_url", PUBLIC_TA)
    monkeypatch.setattr(settings, "nostr_upload_ta_events_relay", INTERNAL_TA)
    monkeypatch.setattr(settings, "trusted_list_relay", "")
    monkeypatch.setattr(settings, "dictionary_relay", DICTIONARY)
    monkeypatch.setattr(
        "app.services.duty_menu_service.assistant_relay_list",
        lambda: [PUBLIC_TA, *OUTBOX],
    )


def _sdk(url: str) -> str:
    """A relay as nostr-sdk reports it: the scheme and host lowercased, a default
    port dropped, the path as given."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port and parts.port != {"ws": 80, "wss": 443}[parts.scheme.lower()]:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), host, parts.path, parts.query, ""))


class Relays:
    """Every relay the service talks to, faked as one: what they hold, which
    are down, and what each publish was sent to."""

    def __init__(self) -> None:
        self.held: list[Event] = []
        self.down: set[str] = set()
        self.refusing: dict[str, str] = {}
        self.added: list[str] = []
        self.read_from: list[str] = []
        self.sent: list[tuple[list[str], Event]] = []

    def client(self, *_, **__):
        relays = self

        class _FakeClient:
            def __init__(self) -> None:
                self.pending: list[str] = []

            async def add_relay(self, url):
                relays.added.append(url)
                self.pending.append(url)
                return True

            async def try_connect(self, _timeout):
                tried, self.pending = self.pending, []
                return SimpleNamespace(
                    success=[_sdk(u) for u in tried if u not in relays.down],
                    failed={
                        _sdk(u): "IO error: Connection refused"
                        for u in tried
                        if u in relays.down
                    },
                )

            async def fetch_events_from(self, urls, _flt, _timeout):
                relays.read_from.extend(urls)
                return SimpleNamespace(to_vec=lambda: list(relays.held))

            async def send_event_to(self, urls, event):
                relays.sent.append((list(urls), event))
                relays.held.append(event)
                return SimpleNamespace(
                    success=[_sdk(u) for u in urls if u not in relays.refusing],
                    failed={
                        _sdk(u): m for u, m in relays.refusing.items() if u in urls
                    },
                )

            async def disconnect(self):
                pass

        return _FakeClient()


@pytest.fixture
def relays(monkeypatch) -> Relays:
    fake = Relays()
    monkeypatch.setattr("app.services.duty_menu_service.Client", fake.client)
    return fake


@pytest.fixture
def menu_client(client, monkeypatch, assistant):
    """The duty-menu routes with the Assistant key read stubbed to `assistant`."""
    row = SimpleNamespace(nsec=assistant.secret_key().to_bech32())
    monkeypatch.setattr(
        "app.services.duty_menu_service.get_or_create_brainstorm_observer_nsec_by_pubkey_on_db",
        AsyncMock(return_value=(row, False)),
    )

    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[get_db] = _fake_db
    yield client
    app.dependency_overrides.pop(get_db, None)


def _offer(client) -> dict:
    r = client.get("/user/assistantDutyMenu")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _publish(client) -> dict:
    r = client.post("/user/assistantDutyMenu")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _menu(assistant: Keys, created_at: int, rows: list[list[str]]) -> Event:
    hex_pubkey = assistant.public_key().to_hex()
    return (
        EventBuilder(Kind(39999), "")
        .tags([Tag.parse(t) for t in build_menu_tags(hex_pubkey, rows)])
        .custom_created_at(Timestamp.from_secs(created_at))
        .allow_self_tagging()
        .sign_with_keys(assistant)
    )


# --- GET: the offer ---------------------------------------------------------


def test_get_is_the_offer_not_a_user_profile(menu_client, monkeypatch, assistant):
    # `/user/{pubkey}` is a single-segment catch-all under the same prefix; a
    # route registered after it would answer this path with a profile and a 200.
    profile_lookup = AsyncMock(side_effect=AssertionError("hit /user/{pubkey}"))
    monkeypatch.setattr("app.routers.user.router.get_user_graph_data", profile_lookup)

    offer = _offer(menu_client)

    assert set(offer) == {"assistant", "rows"}
    assert offer["assistant"] == assistant.public_key().to_hex()
    profile_lookup.assert_not_called()


@pytest.mark.parametrize("method", ["get", "post"])
def test_the_menu_routes_require_authentication(method):
    response = getattr(TestClient(app), method)("/user/assistantDutyMenu")
    assert response.status_code in (401, 403)


def test_the_offer_is_the_setup_rows_in_order(menu_client, monkeypatch, assistant):
    """One list feeds both: `/setup`'s rows minus the Assistant column."""
    row = MagicMock()
    row.nsec = assistant.secret_key().to_bech32()
    monkeypatch.setattr(
        setup_router,
        "select_brainstorm_nsec_by_pubkey_on_db",
        AsyncMock(return_value=row),
    )

    setup_rows = menu_client.get(f"/setup/{'a' * 64}").json()
    offer = _offer(menu_client)

    assert offer["rows"] == [[key, relay] for key, _, relay in setup_rows]
    assert [key for key, _ in offer["rows"]] == EXPECTED_KEYS


def test_the_offer_names_public_relays(menu_client):
    rows = dict((k, r) for k, r in _offer(menu_client)["rows"])
    assert rows["30382:rank"] == PUBLIC_TA
    assert rows["30392"] == PUBLIC_TA
    assert rows["39998"] == rows["39999"] == DICTIONARY
    assert INTERNAL_TA not in rows.values()


def test_a_row_with_no_relay_is_not_offered(menu_client, monkeypatch):
    """A `duty` row without a relay is invalid (§ 5.1), so neither side lists it."""
    monkeypatch.setattr(settings, "dictionary_relay", "")
    keys = [key for key, _ in _offer(menu_client)["rows"]]
    assert "39998" not in keys and "39999" not in keys


# --- POST: the signed menu --------------------------------------------------


def test_the_menu_is_signed_by_the_assistant_and_shaped_exactly(
    menu_client, relays, assistant
):
    offer = _offer(menu_client)
    event = _publish(menu_client)["event"]
    who = assistant.public_key().to_hex()

    assert Event.from_json(json.dumps(event)).verify()
    assert event["pubkey"] == who
    assert event["kind"] == 39999
    assert event["content"] == ""
    assert event["tags"] == [
        ["d", "duty-menu"],
        ["z", DUTY_MENU_HEADER],
        ["p", who],
        *(["duty", key, relay] for key, relay in offer["rows"]),
        ["client", "Brainstorm"],
    ]
    assert is_duty_menu(event, who)


def test_the_published_rows_are_the_offer(menu_client, relays):
    """The UI's "up to date" test: the same (key, relay) set on both sides."""
    offer = _offer(menu_client)
    event = _publish(menu_client)["event"]
    duty = [[t[1], t[2]] for t in event["tags"] if t[0] == "duty"]
    assert duty == offer["rows"]
    assert len(duty) == 8


def test_no_menu_semantics_beyond_rows(menu_client, relays):
    """A per-customer menu: no storefront, exceptions, name or details (§ 7)."""
    event = _publish(menu_client)["event"]
    names = {t[0] for t in event["tags"]}
    assert names == {"d", "z", "p", "duty", "client"}
    assert len([t for t in event["tags"] if t[0] == "p"]) == 1


def test_created_at_is_now_when_nothing_newer_is_out(menu_client, relays, assistant):
    relays.held.append(_menu(assistant, 1_000_000_000, []))
    before = int(time.time())
    event = _publish(menu_client)["event"]
    assert before <= event["created_at"] <= int(time.time())


def test_created_at_is_bumped_past_the_newest_menu_found(
    menu_client, relays, assistant
):
    ahead = int(time.time()) + 1000
    relays.held.extend([_menu(assistant, ahead - 5, []), _menu(assistant, ahead, [])])
    event = _publish(menu_client)["event"]
    assert event["created_at"] == ahead + 1


def test_a_second_publish_replaces_the_first(menu_client, relays):
    first = _publish(menu_client)["event"]
    second = _publish(menu_client)["event"]
    assert second["created_at"] > first["created_at"]
    d = lambda e: next(t[1] for t in e["tags"] if t[0] == "d")  # noqa: E731
    assert d(second) == d(first) == "duty-menu"
    assert second["pubkey"] == first["pubkey"]


# --- POST: where it goes ----------------------------------------------------


def test_it_is_sent_to_every_place_8_1_names(menu_client, relays, assistant):
    relays.held.append(
        _menu(assistant, 1_000_000_000, [["30382:rank", "wss://old-scores.example/"]])
    )

    _publish(menu_client)

    ((urls, _),) = relays.sent
    # Each row's relay, the TA relay written at its internal address as scores are.
    assert INTERNAL_TA in urls and PUBLIC_TA not in urls
    assert DICTIONARY in urls
    # The outbox, an earlier version's relay, and Nous' relay.
    assert set(OUTBOX) <= set(urls)
    assert "wss://old-scores.example/" in urls
    assert NOUS in urls
    assert len(urls) == len(set(urls))


def test_earlier_versions_are_looked_for_before_dating_the_new_one(menu_client, relays):
    _publish(menu_client)
    assert INTERNAL_TA in relays.read_from
    assert NOUS in relays.read_from


def test_the_report_names_public_addresses_one_line_per_relay(menu_client, relays):
    relays.down.add("wss://relay.primal.net")
    relays.refusing["wss://relay.damus.io"] = "blocked: rate-limited"

    report = {r["url"]: r for r in _publish(menu_client)["relays"]}

    assert set(report) == {PUBLIC_TA, DICTIONARY, NOUS, *OUTBOX}
    assert report[PUBLIC_TA]["ok"] is True
    assert report[DICTIONARY]["ok"] is True
    assert report[NOUS]["ok"] is True
    assert report["wss://relay.primal.net"] == {
        "url": "wss://relay.primal.net",
        "ok": False,
        "message": "IO error: Connection refused",
    }
    assert report["wss://relay.damus.io"]["message"] == "blocked: rate-limited"


def test_a_relay_spelled_with_its_default_port_is_sent_to(
    menu_client, relays, monkeypatch
):
    """nostr-sdk reports `wss://Relay.Example:443` as `wss://relay.example`."""
    spelled = "wss://Relay.Example:443"
    monkeypatch.setattr(
        "app.services.duty_menu_service.assistant_relay_list",
        lambda: [PUBLIC_TA, spelled],
    )
    report = {r["url"]: r for r in _publish(menu_client)["relays"]}
    assert report[spelled]["ok"] is True
    ((urls, _),) = relays.sent
    assert spelled in urls


def test_a_menu_no_relay_took_is_an_error(menu_client, relays):
    relays.down.update([INTERNAL_TA, DICTIONARY, NOUS, *OUTBOX])
    r = menu_client.post("/user/assistantDutyMenu")
    assert r.status_code == 502
    assert isinstance(r.json()["detail"], str)


# --- The pure pieces ----------------------------------------------------------


AUTHOR = "b" * 64


def _ev(**over):
    event = {
        "kind": 39999,
        "pubkey": AUTHOR,
        "tags": [["d", "duty-menu"], ["z", DUTY_MENU_HEADER], ["p", AUTHOR]],
    }
    event.update(over)
    return event


def test_6_1_accepts_a_menu():
    assert is_duty_menu(_ev(), AUTHOR)


@pytest.mark.parametrize(
    "event",
    [
        _ev(kind=39998),
        _ev(pubkey="c" * 64),
        _ev(tags=[["d", "other"], ["z", DUTY_MENU_HEADER], ["p", AUTHOR]]),
        _ev(tags=[["d", "duty-menu"], ["p", AUTHOR]]),
        _ev(tags=[["d", "duty-menu"], ["z", DUTY_MENU_HEADER]]),
        _ev(tags=[["d", "duty-menu"], ["z", DUTY_MENU_HEADER], ["p", "c" * 64]]),
        _ev(
            tags=[
                ["d", "duty-menu"],
                ["z", DUTY_MENU_HEADER],
                ["p", AUTHOR],
                ["p", AUTHOR],
            ]
        ),
        _ev(
            tags=[
                ["d", "duty-menu"],
                ["z", DUTY_MENU_HEADER],
                ["p", AUTHOR],
                ["z", "list"],
            ]
        ),
        _ev(
            tags=[
                ["d", "duty-menu"],
                ["z", DUTY_MENU_HEADER],
                ["p", AUTHOR],
                ["z", f"39998:{'c' * 64}:concept-header"],
            ]
        ),
    ],
    ids=[
        "kind",
        "author",
        "d",
        "no-z",
        "no-p",
        "p-not-author",
        "two-p",
        "z-list",
        "concept-header-z",
    ],
)
def test_6_1_rejects(event):
    assert not is_duty_menu(event, AUTHOR)


def test_created_at_rule():
    assert menu_created_at(100, []) == 100
    assert menu_created_at(100, [{"created_at": 50}]) == 100
    assert menu_created_at(100, [{"created_at": 100}]) == 101
    assert menu_created_at(100, [{"created_at": 300}, {"created_at": 200}]) == 301


def test_publish_targets_are_each_relay_once():
    targets = publish_targets(
        [PUBLIC_TA, DICTIONARY], [PUBLIC_TA + "/", "wss://relay.damus.io"], [NOUS]
    )
    assert [(t.url, t.write_url) for t in targets] == [
        (PUBLIC_TA, INTERNAL_TA),
        (DICTIONARY, DICTIONARY),
        ("wss://relay.damus.io", "wss://relay.damus.io"),
        (NOUS, NOUS),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "wss://R.Example:443",
        "wss://r.example:443/",
        "ws://r.example:80/x/",
        "wss://r.example/Path/?q=1",
        "wss://r.example:8443/",
        "WSS://r.example",
        "ws://127.0.0.1:7777/",
    ],
)
def test_relay_key_matches_nostr_sdk_s_spelling(url):
    """nostr-sdk reports a relay as the url crate spells it, and the key has to
    match that to know which targets connected."""

    async def sdk_spelling() -> str:
        client = Client()
        await client.add_relay(url)
        return list((await client.relays()).keys())[0]

    assert relay_key(asyncio.run(sdk_spelling())) == relay_key(url)


def test_relay_key_keeps_what_tells_relays_apart():
    assert relay_key("wss://r.example:8443") != relay_key("wss://r.example")
    assert relay_key("wss://r.example/a") != relay_key("wss://r.example/A")
    assert relay_key("wss://r.example/?a=1") != relay_key("wss://r.example")
    assert relay_key("ws://r.example") != relay_key("wss://r.example")


def test_the_public_ta_relay_with_its_default_port_is_written_internally():
    assert write_url(PUBLIC_TA + ":443/") == INTERNAL_TA
