"""Your Assistant's Treasure Map: `GET` and `POST /user/assistantTreasureMap`.

Brainstorm-UI counts the Map up to date when the self-entries it finds, as a set
of (key, relay), equal the GET's `shouldSay`, so the assertions here are on exact
shape. The grammar cases are ported from Tapestry's
`test/assistant-self-map-check.test.js` and `assistant-self-map-publish.test.js`
(their ids in brackets), with Brainstorm's per-key offer. The relays are faked at
the service's import site; signing is real, with a generated Assistant key.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from nostr_sdk import Event, EventBuilder, Keys, Kind, Tag, Timestamp

from app.api import app
from app.core.config import settings
from app.core.database import get_db
from app.services import assistant_publish, self_map_service
from app.services.duty_menu_build import designation_rows, offer_rows
from app.services.self_map_build import (
    ScoresUpkeep,
    asks_for_scores,
    compare_keys,
    evaluate_self_map,
    map_groups,
    most_specific_first,
    newest,
    nothing_to_change,
    parse_key,
    read_self_entries,
    same_self_entries,
    scores_upkeep,
    self_map_relays,
    self_map_tags,
)
from app.utils.datetimes import utc_now

PUBLIC_TA = "wss://scores.example"
INTERNAL_TA = "ws://strfry.internal:7777"
DICTIONARY = "wss://dcosl.brainstorm.world"
OUR_RELAY = "ws://neofry.internal:7777"
OUTBOX = ["wss://relay.damus.io", "wss://relay.primal.net"]

FIVE = [
    "30382:rank",
    "30382:followers",
    "30382:reporters",
    "30382:muters",
    "30382:hops",
]

A = "a" * 64  # an Assistant, for the pure cases
B = "b" * 64  # another provider
LIVE = ScoresUpkeep("live")


@pytest.fixture(autouse=True)
def relay_settings(monkeypatch):
    monkeypatch.setattr(settings, "nostr_upload_ta_events_relay_public_url", PUBLIC_TA)
    monkeypatch.setattr(settings, "nostr_upload_ta_events_relay", INTERNAL_TA)
    monkeypatch.setattr(settings, "nostr_transfer_to_relay", OUR_RELAY)
    monkeypatch.setattr(settings, "trusted_list_relay", "")
    monkeypatch.setattr(settings, "dictionary_relay", DICTIONARY)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(
        "app.services.self_map_service.assistant_relay_list",
        lambda: [PUBLIC_TA, *OUTBOX],
    )


def _check(tags, scores=LIVE, rows=None):
    """What A's Treasure Map should list under a reader's Map with these tags."""
    return evaluate_self_map(
        map_groups({"kind": 10040, "tags": tags} if tags is not None else None),
        offer_rows(A) if rows is None else rows,
        A,
        scores,
    )


def _lines(check) -> dict:
    return {(line.key, line.status): line for line in check.duties}


def _five(relay=PUBLIC_TA):
    return {(key, relay) for key in FIVE}


def _seconds(naive_utc) -> int:
    return int(naive_utc.replace(tzinfo=timezone.utc).timestamp())


# --- What it should list -----------------------------------------------------


def test_setup_s_map_with_scores_live_lists_exactly_the_five_score_rows():
    """[U14] The Map `/setup` publishes is claimed verbatim, entry for entry."""
    check = _check(designation_rows(A))

    # In the offer's order, though order never counts.
    assert check.should_say == [(key, PUBLIC_TA) for key in FIVE]
    lines = check.duties
    assert [line.key for line in lines] == [*FIVE, "30392", "39998", "39999"]
    for line in lines[:5]:
        assert line.status == "live" and line.relay == PUBLIC_TA and line.reason is None
        assert line.entries == ((line.key, PUBLIC_TA),)
    assert (lines[5].status, lines[5].reason) == ("offered", "not-scheduled")
    for line in lines[6:]:
        assert (line.status, line.relay, line.reason) == (
            "offered",
            DICTIONARY,
            "not-published-here",
        )
        assert line.entries == ()


@pytest.mark.parametrize(
    "status, last_landed_at",
    [("not-landed", None), ("not-scheduled", None), ("upkeep-stopped", 1_700_000_000)],
)
def test_scores_that_are_not_live_are_listed_nowhere(status, last_landed_at):
    """[U6, U8, U7] A self-Map says less rather than more (§ 9.2)."""
    check = _check(designation_rows(A), ScoresUpkeep(status, last_landed_at))

    assert check.should_say == []
    for line in check.duties[:5]:
        assert (line.status, line.reason) == ("offered", status)
        assert line.last_landed_at == last_landed_at
        assert line.entries == ()


def test_a_bare_30382_narrows_to_one_key_per_published_metric():
    """[U13] The offer has no `30382` row, so it never gives `30382` a relay."""
    check = _check([["30382", A, PUBLIC_TA]])

    assert set(check.should_say) == _five()
    assert [line.key for line in check.duties] == FIVE
    assert all(line.status == "live" for line in check.duties)


def test_a_30382_metric_brainstorm_does_not_publish_is_not_offered():
    check = _check([["30382:verifiedFollowersCount", A, PUBLIC_TA]])

    assert check.should_say == []
    assert [(line.key, line.status) for line in check.duties] == [
        ("30382:verifiedFollowersCount", "not-offered")
    ]


@pytest.mark.parametrize("key", ["30383", "30023"])
def test_a_kind_the_offer_has_no_row_for_is_not_offered(key):
    """[U38] Another Score kind, or a kind outside Scores, Lists and Concepts."""
    check = _check([[key, A, PUBLIC_TA]])

    assert check.should_say == []
    assert [(line.key, line.status, line.relay) for line in check.duties] == [
        (key, "not-offered", None)
    ]


def test_a_bare_star_is_never_claimed_verbatim():
    """[U16] Its meets give the five Score rows; Lists and Concepts aren't live,
    and the rest of `*` isn't offered."""
    check = _check([["*", A, PUBLIC_TA]])

    assert set(check.should_say) == _five()
    lines = _lines(check)
    assert all(lines[(key, "live")].entries == ((key, PUBLIC_TA),) for key in FIVE)
    assert lines[("30392", "offered")].reason == "not-scheduled"
    assert lines[("39998", "offered")].reason == "not-published-here"
    assert lines[("39999", "offered")].reason == "not-published-here"
    assert ("*", "not-offered") in lines


def test_3038x_narrows_to_the_score_rows_and_says_the_rest_is_not_offered():
    check = _check([["3038x", A, PUBLIC_TA]])

    assert set(check.should_say) == _five()
    assert [(line.key, line.status) for line in check.duties] == [
        *((key, "live") for key in FIVE),
        ("3038x", "not-offered"),
    ]


def test_an_alternate_has_the_duty_too():
    """[U15] § 9: Preferred or Alternate."""
    check = _check([["30382:rank", B, "wss://b.example"], ["30382:rank", A, PUBLIC_TA]])

    assert check.should_say == [("30382:rank", PUBLIC_TA)]


def test_a_more_specific_entry_for_another_key_is_elsewhere_and_never_listed():
    check = _check([["*", A, PUBLIC_TA], ["30382:rank", B, "wss://b.example"]])

    assert set(check.should_say) == _five() - {("30382:rank", PUBLIC_TA)}
    lines = _lines(check)
    assert ("30382:rank", "elsewhere") in lines
    assert ("30382:rank", "live") not in lines


def test_lists_are_never_claimed():
    """[U17] Only POST /admin/trustedLists publishes them: nothing keeps them up to date."""
    check = _check([["30392", A, PUBLIC_TA]])

    assert check.should_say == []
    assert [(line.key, line.status, line.reason) for line in check.duties] == [
        ("30392", "offered", "not-scheduled")
    ]


@pytest.mark.parametrize(
    "key, parts",
    [
        # A bare 39998 covers both Concept kinds, and the offer has a row for each.
        ("39998", ["39998", "39999"]),
        ("39999", ["39999"]),
        ("39998:restaurants", ["39998:restaurants"]),
        ("39999:restaurants", ["39999:restaurants"]),
    ],
)
def test_concepts_are_never_claimed(key, parts):
    """[U18] The Assistant publishes no headers or items yet."""
    check = _check([[key, A, DICTIONARY]])

    assert check.should_say == []
    assert [(line.key, line.status, line.reason) for line in check.duties] == [
        (part, "offered", "not-published-here") for part in parts
    ]


def test_the_concept_reading():
    """[U19] A bare `39998` covers `39999:<d>`; a `39999` key beats a bare `39998`."""
    bare, item = parse_key("39998"), parse_key("39999:x")
    assert 39999 in bare.kinds
    assert compare_keys(parse_key("39999"), bare) < 0
    assert compare_keys(item, bare) < 0
    assert parse_key("39998:dlist-header").norm == "39998"


def test_an_unchecked_key_is_never_claimed():
    """[U20] A key shape the engine doesn't reason over."""
    under_a_row = _check([["30392:tag:x", A, PUBLIC_TA]])
    assert under_a_row.should_say == []
    assert [(line.key, line.reason) for line in under_a_row.duties] == [
        ("30392:tag:x", "unchecked")
    ]

    no_row = _check([["30382:tag", A, PUBLIC_TA]])
    assert no_row.should_say == []
    assert [(line.key, line.status) for line in no_row.duties] == [
        ("30382:tag", "not-offered")
    ]


def test_an_unchecked_key_keeps_a_broader_one_from_claiming_it():
    """[U20] Under an offer with a bare `30382` row (Tapestry's shape), `30382`
    would win `30382:tag`, which isn't live, so the metrics are listed one by one."""
    check = _check(
        [["30382", A, PUBLIC_TA], ["30382:tag", A, PUBLIC_TA]],
        rows=[["30382", PUBLIC_TA]],
    )
    assert set(check.should_say) == _five()

    without = _check([["30382", A, PUBLIC_TA]], rows=[["30382", PUBLIC_TA]])
    assert without.should_say == [("30382", PUBLIC_TA)]


def test_no_map_or_a_map_naming_only_others_has_no_duties():
    """[U23]"""
    for tags in (None, [], [["30382:rank", B, PUBLIC_TA]]):
        check = _check(tags)
        assert check.duties == [] and check.should_say == []


def test_the_spellings_that_name_nothing_change_nothing():
    """[U39, U40] `39998:`, `39999:`, `39999:dlist-header`, `39999:duty-menu`."""
    nameless = [
        [key, A, ""]
        for key in ("39998:", "39999:", "39999:dlist-header", "39999:duty-menu")
    ]

    assert _check(designation_rows(A) + nameless) == _check(designation_rows(A))
    alone = _check(nameless + [["39998:", B, ""]])
    assert alone.duties == [] and alone.should_say == []


# --- The grammar -------------------------------------------------------------


def test_compare_keys_puts_the_most_specific_first():
    """[L3] An exact kind, then a family, then `*`; then a value before an empty segment."""
    keys = [
        "*",
        "3039x",
        "30396:tag",
        "3039x:tag",
        "30396",
        "30396:tag::x",
        "30396:tag:d",
    ]
    ordered = [k.norm for k in sorted(map(parse_key, keys), key=most_specific_first)]
    assert ordered == [
        "30396:tag:d",
        "30396:tag::x",
        "30396:tag",
        "30396",
        "3039x:tag",
        "3039x",
        "*",
    ]


def test_parse_key_reads_the_nameless_and_concept_spellings():
    """[L7]"""
    for key in (
        "39998:",
        "39999:",
        "39999:dlist-header",
        "39999:duty-menu",
        "3040x",
        "abc",
        7,
    ):
        assert parse_key(key) is None
    assert parse_key("39998:duty-menu").norm == "39998:duty-menu"
    assert parse_key("39998::").scope.value == ":"
    assert parse_key("30382:rank:").norm == "30382:rank"
    assert parse_key("3038x:rank").scope.type == "metric"
    assert parse_key("*:rank").scope.type == "unchecked"


def test_a_kind_slot_is_ascii_digits():
    """`\\d` and `int()` read fullwidth digits, so `３０３８２` would pass for 30382."""
    fullwidth = "３０３８２:rank"
    assert parse_key(fullwidth) is None
    assert read_self_entries({"tags": [[fullwidth, A, PUBLIC_TA]]}, A) == []
    check = _check([[fullwidth, A, PUBLIC_TA]])
    assert check.duties == [] and check.should_say == []


def test_read_self_entries():
    """[L4, L6, L8] Entries naming the author whose key parses, normalized, each
    once; element 3 missing or empty is `""`."""
    event = {
        "tags": [
            ["30382:rank:", A.upper(), " wss://scores.example "],
            ["30382:rank", A, "wss://scores.example"],
            ["39998:dlist-header", A],
            ["39998:", A, "wss://x"],
            ["30382:rank", B, PUBLIC_TA],
            ["p", A],
            ["d", A, "wss://x"],
        ]
    }
    assert read_self_entries(event, A) == [
        ("30382:rank", "wss://scores.example"),
        ("39998", ""),
    ]


def test_same_self_entries_is_a_set_of_key_and_relay():
    """[L5]"""
    one = [("30382:rank", PUBLIC_TA), ("30382:hops", PUBLIC_TA)]
    assert same_self_entries(one, list(reversed(one)))
    assert same_self_entries(
        [("30382:rank", PUBLIC_TA + "/")], [("30382:rank", PUBLIC_TA)]
    )
    assert not same_self_entries(one, one[:1])
    assert not same_self_entries(one[:1], [("30382:rank", "wss://elsewhere")])
    assert same_self_entries([], [])


def test_a_version_s_tags_are_its_self_entries_alone():
    """Only this server signs the Assistant's kind 10040, so there is nothing
    else to keep."""
    assert self_map_tags(A.upper(), [("30382:rank", PUBLIC_TA)]) == [
        ["30382:rank", A, PUBLIC_TA]
    ]
    assert self_map_tags(A, []) == []


def test_nothing_to_change():
    """[E3] The same self-entries, in any order."""
    target = [("30382:rank", PUBLIC_TA), ("30382:hops", PUBLIC_TA)]
    same = {"tags": [["30382:hops", A, PUBLIC_TA], ["30382:rank", A, PUBLIC_TA]]}

    assert nothing_to_change(same, A, target)
    assert nothing_to_change(None, A, [])
    assert not nothing_to_change(None, A, target)
    assert not nothing_to_change(same, A, [])
    # Off, with only a spelling readers ignore: it claims nothing.
    assert nothing_to_change({"tags": [["39998:", A, ""]]}, A, [])


def test_one_missed_run_is_a_brief_gap_and_two_a_stop():
    """[U7] With a 12-hour schedule: 23 hours ago is live, 25 is stopped."""
    now = utc_now()
    twelve_hours = 12 * 3600
    assert scores_upkeep(now - timedelta(hours=23), twelve_hours, now).status == "live"
    landed = now - timedelta(hours=25)
    assert scores_upkeep(landed, twelve_hours, now) == ScoresUpkeep(
        "upkeep-stopped", _seconds(landed)
    )
    assert scores_upkeep(None, twelve_hours, now) == ScoresUpkeep("not-landed")
    assert scores_upkeep(now, None, now).status == "not-scheduled"


@pytest.mark.parametrize(
    "tags, asks",
    [
        (None, False),
        ([["30382:rank", B, PUBLIC_TA]], False),
        ([["30392", A, PUBLIC_TA], ["39998", A, DICTIONARY]], False),
        ([["30383:rank", A, PUBLIC_TA]], False),
        ([["30382:rank", A, PUBLIC_TA]], True),
        ([["3038x", A, PUBLIC_TA]], True),
        ([["*", A, PUBLIC_TA]], True),
    ],
    ids=["no-map", "others", "no-score-kind", "30383", "30382", "3038x", "star"],
)
def test_the_upkeep_counts_only_when_the_map_asks_for_scores(tags, asks):
    """So the follow count is read only then: when no key naming the Assistant
    covers 30382, no Score is its duty, and the check is the same live or not."""
    event = {"kind": 10040, "tags": tags} if tags is not None else None
    assert asks_for_scores(map_groups(event), A) is asks
    stopped = ScoresUpkeep("upkeep-stopped", 1_700_000_000)
    assert (_check(tags, LIVE) == _check(tags, stopped)) is not asks


def test_the_relays_are_the_outbox_then_should_say_then_hints_each_once():
    relays = self_map_relays(
        [PUBLIC_TA, *OUTBOX],
        [("30382:rank", PUBLIC_TA + "/")],
        [DICTIONARY, "", "not a relay", "wss://relay.damus.io/"],
    )
    assert relays == [PUBLIC_TA, *OUTBOX, DICTIONARY]


def test_newest_is_nip_01_s():
    """The latest created_at, and on a tie the lowest id."""
    events = [
        {"created_at": 5, "id": "b"},
        {"created_at": 5, "id": "a"},
        {"created_at": 4, "id": "0"},
    ]
    assert newest(events)["id"] == "a"
    assert newest([]) is None


# --- The routes ----------------------------------------------------------------


def _version(
    keys: Keys, tags: list[list[str]], created_at: int, content: str = ""
) -> dict:
    """A kind 10040 signed by `keys`, as a relay returns it."""
    event = (
        EventBuilder(Kind(10040), content)
        .tags([Tag.parse(t) for t in tags])
        .custom_created_at(Timestamp.from_secs(created_at))
        .allow_self_tagging()
        .sign_with_keys(keys)
    )
    return json.loads(event.as_json())


def _sdk(url: str) -> str:
    """A relay as nostr-sdk reports it: the scheme and host lowercased, a default
    port dropped, the path as given."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port and parts.port != {"ws": 80, "wss": 443}[parts.scheme.lower()]:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), host, parts.path, parts.query, ""))


class World:
    """The caller's records and every relay the service talks to, faked: the
    caller's Map on our relay, what each relay holds of the Assistant's kind
    10040, which relays are silent or down, and what each publish was sent to."""

    def __init__(self, user: str, assistant: Keys) -> None:
        self.user = user
        self.assistant = assistant
        self.reader_map: dict | None = None
        self.map_unreadable = False
        self.held: dict[str, list[dict]] = {}
        self.silent: set[str] = set()
        self.read_from: list[str] = []
        self.down: set[str] = set()
        self.refusing: dict[str, str] = {}
        self.sent: list[tuple[list[str], dict]] = []
        self.choices: list[bool] = []
        # What happened, in order: "sent", "saved", "committed".
        self.log: list[str] = []
        self.db = MagicMock(
            commit=AsyncMock(side_effect=lambda: self.log.append("committed"))
        )
        self.row = SimpleNamespace(
            pubkey=user,
            nsec=assistant.secret_key().to_bech32(),
            treasure_map_on=False,
            last_time_published_graperank=utc_now() - timedelta(hours=1),
            scheduling_id=None,
        )
        self.policy = SimpleNamespace(enabled=True, schedule_interval_seconds=12 * 3600)
        self.default_policy: SimpleNamespace | None = self.policy
        # The caller's own policy, when `row.scheduling_id` names one.
        self.own_policy: SimpleNamespace | None = None
        # The follow count: None when Neo4j fails it, and whether Neo4j stalls.
        self.follows: int | None = 3
        self.neo4j_stalls = False
        self.follow_reads = 0

    @property
    def hex(self) -> str:
        return self.assistant.public_key().to_hex()

    def setup_map(self) -> None:
        """The caller's Map is what `/setup` publishes for this Assistant."""
        self.reader_map = {
            "kind": 10040,
            "pubkey": self.user,
            "created_at": 1_000,
            "content": "",
            "tags": designation_rows(self.hex),
        }

    async def fetch_reader_map(self, relay: str, author: str):
        assert relay == OUR_RELAY and author == self.user
        if self.map_unreadable:
            raise ConnectionError("down")
        return self.reader_map

    async def fetch_latest(self, relay: str, flt: dict):
        assert flt["kinds"] == [10040] and flt["authors"] == [self.hex]
        self.read_from.append(relay)
        if relay in self.silent:
            raise TimeoutError()
        return newest(self.held.get(relay, []))

    def client(self, *_, **__):
        world = self

        class _FakeClient:
            def __init__(self) -> None:
                self.pending: list[str] = []

            async def add_relay(self, url):
                self.pending.append(url)
                return True

            async def try_connect(self, _timeout):
                tried, self.pending = self.pending, []
                return SimpleNamespace(
                    success=[_sdk(u) for u in tried if u not in world.down],
                    failed={
                        _sdk(u): "IO error: Connection refused"
                        for u in tried
                        if u in world.down
                    },
                )

            async def send_event_to(self, urls, event):
                signed = json.loads(event.as_json())
                world.sent.append((list(urls), signed))
                world.log.append("sent")
                await asyncio.sleep(0)
                for url in urls:
                    if url not in world.refusing:
                        world.held.setdefault(url, []).append(signed)
                return SimpleNamespace(
                    success=[_sdk(u) for u in urls if u not in world.refusing],
                    failed={_sdk(u): m for u, m in world.refusing.items() if u in urls},
                )

            async def disconnect(self):
                pass

        return _FakeClient()


@pytest.fixture
def assistant() -> Keys:
    return Keys.generate()


@pytest.fixture
def world(monkeypatch, caller, assistant) -> World:
    w = World(caller.pubkey, assistant)
    w.setup_map()
    monkeypatch.setattr(
        self_map_service, "fetch_latest_designation", w.fetch_reader_map
    )
    monkeypatch.setattr(assistant_publish, "fetch_latest", w.fetch_latest)
    monkeypatch.setattr(assistant_publish, "Client", w.client)
    monkeypatch.setattr(
        assistant_publish,
        "get_or_create_brainstorm_observer_nsec_by_pubkey_on_db",
        AsyncMock(side_effect=lambda db, pubkey: (w.row, False)),
    )
    monkeypatch.setattr(
        self_map_service,
        "get_default_scheduling_on_db",
        AsyncMock(side_effect=lambda db: w.default_policy),
    )
    monkeypatch.setattr(
        self_map_service,
        "get_scheduling_on_db",
        AsyncMock(
            side_effect=lambda db, scheduling_id: (
                w.own_policy if scheduling_id == w.row.scheduling_id else None
            )
        ),
    )

    async def grapevine(pubkey):
        assert pubkey == w.user
        w.follow_reads += 1
        if w.neo4j_stalls:
            await asyncio.sleep(3)
        if w.follows is None:
            raise ConnectionError("neo4j down")
        return w.follows > 0

    monkeypatch.setattr(self_map_service, "has_grapevine", grapevine)

    async def remember(db, pubkey, on):
        assert pubkey == w.user and db is w.db
        w.choices.append(on)
        w.log.append("saved")

    monkeypatch.setattr(
        self_map_service, "set_treasure_map_on_by_pubkey_on_db", remember
    )
    return w


@pytest.fixture
def map_client(client, world):
    async def _fake_db():
        yield world.db

    app.dependency_overrides[get_db] = _fake_db
    yield client
    app.dependency_overrides.pop(get_db, None)


def _get(client) -> dict:
    r = client.get("/user/assistantTreasureMap")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _post(client, on: bool) -> dict:
    r = client.post("/user/assistantTreasureMap", json={"on": on})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _self_entries(event: dict, who: str) -> set[tuple[str, str]]:
    return {(t[0], t[2]) for t in event["tags"] if len(t) > 2 and t[1] == who}


def test_get_is_the_check_not_a_user_profile(map_client, monkeypatch):
    profile_lookup = AsyncMock(side_effect=AssertionError("hit /user/{pubkey}"))
    monkeypatch.setattr("app.routers.user.router.get_user_graph_data", profile_lookup)

    _get(map_client)

    profile_lookup.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_the_routes_require_authentication(method):
    body = {"on": True} if method == "POST" else None
    response = TestClient(app).request(method, "/user/assistantTreasureMap", json=body)
    assert response.status_code in (401, 403)


def test_the_get_is_the_shape_the_ui_parses(map_client, world):
    check = _get(map_client)

    assert set(check) == {"assistant", "on", "duties", "shouldSay", "relays"}
    assert check["assistant"] == world.hex
    assert check["on"] is False
    assert {tuple(e) for e in check["shouldSay"]} == _five()
    assert check["relays"] == [PUBLIC_TA, *OUTBOX, DICTIONARY]
    rank = check["duties"][0]
    assert rank == {
        "key": "30382:rank",
        "status": "live",
        "relay": PUBLIC_TA,
        "reason": None,
        "lastLandedAt": None,
        "entries": [["30382:rank", PUBLIC_TA]],
    }
    assert [d["key"] for d in check["duties"]] == [*FIVE, "30392", "39998", "39999"]


def test_the_get_reads_the_remembered_choice(map_client, world):
    world.row.treasure_map_on = True
    assert _get(map_client)["on"] is True


def test_with_no_map_there_are_no_duties_and_the_relays_are_the_outbox(
    map_client, world
):
    world.reader_map = None
    check = _get(map_client)
    assert check["duties"] == [] and check["shouldSay"] == []
    assert check["relays"] == [PUBLIC_TA, *OUTBOX]


def test_a_map_that_cannot_be_read_is_an_error_not_an_empty_map(map_client, world):
    world.map_unreadable = True
    r = map_client.get("/user/assistantTreasureMap")
    assert r.status_code == 502
    assert isinstance(r.json()["detail"], str)


def _map_with_keys(world, n: int) -> None:
    """The caller's Map names the Assistant under `n` different keys."""
    world.reader_map["tags"] = [[f"*:k{i}", world.hex, PUBLIC_TA] for i in range(n)]


def test_a_map_of_64_keys_is_checked(map_client, world):
    _map_with_keys(world, 64)
    assert len(_get(map_client)["duties"]) > 0


def test_a_map_too_big_to_check_is_a_422_not_a_truncated_check(map_client, world):
    """A check weighs each key against every other: an 835-key Map took
    seconds of the event loop's CPU per GET."""
    _map_with_keys(world, 65)
    r = map_client.get("/user/assistantTreasureMap")
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)


def test_turn_on_over_a_map_too_big_to_check_publishes_nothing(map_client, world):
    _map_with_keys(world, 65)
    r = map_client.post("/user/assistantTreasureMap", json={"on": True})
    assert r.status_code == 422
    assert world.sent == [] and world.choices == []


def test_turn_off_needs_no_map_even_one_too_big(map_client, world):
    tags = [[k, world.hex, PUBLIC_TA] for k in FIVE]
    world.held[INTERNAL_TA] = [_version(world.assistant, tags, 1_700_000_000)]
    _map_with_keys(world, 65)

    event = _post(map_client, False)["event"]
    assert _self_entries(event, world.hex) == set()


@pytest.mark.parametrize(
    "change, status",
    [
        (lambda w: setattr(settings, "scheduler_enabled", False), "not-scheduled"),
        (lambda w: setattr(w.policy, "enabled", False), "not-scheduled"),
        (lambda w: setattr(w, "default_policy", None), "not-scheduled"),
        # The scheduler skips a customer who follows no one (`has_grapevine`).
        (lambda w: setattr(w, "follows", 0), "not-scheduled"),
        (lambda w: setattr(w.row, "last_time_published_graperank", None), "not-landed"),
        (
            lambda w: setattr(
                w.row, "last_time_published_graperank", utc_now() - timedelta(hours=25)
            ),
            "upkeep-stopped",
        ),
        (
            lambda w: setattr(
                w.row, "last_time_published_graperank", utc_now() - timedelta(hours=23)
            ),
            "live",
        ),
    ],
    ids=[
        "scheduler-off",
        "policy-off",
        "no-default-policy",
        "no-follows",
        "never-published",
        "25h",
        "23h",
    ],
)
def test_scores_are_live_only_while_the_scheduler_keeps_them_up_to_date(
    map_client, world, change, status
):
    change(world)
    rank = _get(map_client)["duties"][0]
    assert rank["status"] == ("live" if status == "live" else "offered")
    assert rank["reason"] == (None if status == "live" else status)
    landed = world.row.last_time_published_graperank
    expected = _seconds(landed) if status == "upkeep-stopped" else None
    assert rank["lastLandedAt"] == expected


def test_the_callers_own_policy_wins_over_the_default(map_client, world):
    world.row.scheduling_id = 7
    world.own_policy = SimpleNamespace(enabled=True, schedule_interval_seconds=3600)
    world.row.last_time_published_graperank = utc_now() - timedelta(hours=3)
    assert _get(map_client)["duties"][0]["reason"] == "upkeep-stopped"

    world.own_policy.enabled = False
    assert _get(map_client)["duties"][0]["reason"] == "not-scheduled"


def test_a_follow_count_that_cannot_be_read_is_an_error(map_client, world):
    """Read as none, it would make Turn on take the Scores entries off."""
    world.follows = None
    assert map_client.get("/user/assistantTreasureMap").status_code == 502
    r = map_client.post("/user/assistantTreasureMap", json={"on": True})
    assert r.status_code == 502 and isinstance(r.json()["detail"], str)
    assert world.sent == [] and world.choices == []


def test_a_follow_count_neo4j_never_answers_is_an_error_at_once(
    map_client, world, monkeypatch
):
    """The driver has no query timeout and waits 30 s to connect. Unbounded, a
    press would outlast the UI's 30 s, holding its slot, and then publish."""
    monkeypatch.setattr(self_map_service, "FOLLOWS_TIMEOUT_SECONDS", 0.05)
    world.neo4j_stalls = True
    start = time.monotonic()

    assert map_client.get("/user/assistantTreasureMap").status_code == 502
    r = map_client.post("/user/assistantTreasureMap", json={"on": True})

    assert r.status_code == 502 and isinstance(r.json()["detail"], str)
    assert time.monotonic() - start < 1
    assert world.sent == [] and world.choices == []


def test_the_follow_count_is_read_only_when_the_map_asks_for_scores(map_client, world):
    """Otherwise it changes nothing the check says, so Neo4j being down doesn't
    stop a customer whose Map asks their Assistant for no Scores."""
    world.follows = None
    world.reader_map["tags"] = [
        ["30392", world.hex, PUBLIC_TA],
        ["30382", B, PUBLIC_TA],
    ]
    _get(map_client)
    world.reader_map = None
    _get(map_client)
    _post(map_client, True)
    assert world.follow_reads == 0

    world.setup_map()
    world.follows = 3
    _get(map_client)
    assert world.follow_reads == 1


def test_turn_off_reads_no_follow_count(map_client, world):
    """Turning off lists nothing, so the follow count can't change what's signed
    or where it goes: live Scores would only add the TA relay, which the outbox
    names first."""
    tags = [[k, world.hex, PUBLIC_TA] for k in FIVE]
    world.held[INTERNAL_TA] = [_version(world.assistant, tags, 1_700_000_000)]
    world.follows = None

    published = _post(map_client, False)

    assert world.follow_reads == 0
    assert published["published"] is True
    assert published["event"]["tags"] == [["client", "Brainstorm"]]
    assert sorted(world.read_from) == sorted([INTERNAL_TA, *OUTBOX])
    assert published["relays"] == [
        {"url": url, "ok": True, "message": None} for url in [PUBLIC_TA, *OUTBOX]
    ]
    assert world.choices == [False]


def test_the_house_observer_is_kept_up_to_date_by_the_periodic_trigger(
    map_client, world, monkeypatch, caller
):
    monkeypatch.setattr(settings, "periodic_graperank_pubkey", caller.pubkey)
    monkeypatch.setattr(settings, "scheduler_enabled", False)

    world.row.last_time_published_graperank = utc_now() - timedelta(hours=3)
    assert _get(map_client)["duties"][0]["status"] == "live"
    world.row.last_time_published_graperank = utc_now() - timedelta(hours=5)
    assert _get(map_client)["duties"][0]["reason"] == "upkeep-stopped"


def test_turn_on_publishes_what_the_get_says_signed_by_the_assistant(map_client, world):
    should_say = {tuple(e) for e in _get(map_client)["shouldSay"]}
    published = _post(map_client, True)
    event = published["event"]

    assert published["on"] is True and published["published"] is True
    assert Event.from_json(json.dumps(event)).verify()
    assert (
        event["pubkey"] == world.hex
        and event["kind"] == 10040
        and event["content"] == ""
    )
    # A tag whose element 2 is the signer survives signing.
    assert _self_entries(event, world.hex) == should_say == _five()
    assert event["tags"][-1] == ["client", "Brainstorm"]
    assert len(event["tags"]) == 6
    assert world.choices == [True]


def test_a_version_is_the_self_entries_and_the_client_tag(map_client, world):
    """Only this server signs the Assistant's kind 10040, so a press keeps
    nothing of the version it replaces: not a stale self-entry, not a tag a
    version could carry, not its content."""
    found = _version(
        world.assistant,
        [
            ["30392", world.hex, PUBLIC_TA],
            ["expiration", "4000000000"],
            ["t", "x"],
        ],
        created_at=1_700_000_000,
        content="hello",
    )
    world.held[INTERNAL_TA] = [found]

    event = _post(map_client, True)["event"]

    assert event["content"] == ""
    assert event["created_at"] > found["created_at"]
    assert event["tags"] == [
        *([key, world.hex, PUBLIC_TA] for key in FIVE),
        ["client", "Brainstorm"],
    ]


def test_a_new_version_is_dated_after_the_newest_found(map_client, world):
    ahead = int(time.time()) + 1000
    world.held[INTERNAL_TA] = [_version(world.assistant, [], ahead - 5)]
    world.held[OUTBOX[0]] = [_version(world.assistant, [], ahead)]

    assert _post(map_client, True)["event"]["created_at"] == ahead + 1


def test_the_newest_version_across_relays_is_the_one_compared(map_client, world):
    """[R5] An older version that already says it all is not the newest."""
    says_it_all = [[key, world.hex, PUBLIC_TA] for key in FIVE]
    world.held[INTERNAL_TA] = [_version(world.assistant, says_it_all, 1_700_000_000)]
    world.held[OUTBOX[1]] = [_version(world.assistant, [], 1_700_000_100)]

    published = _post(map_client, True)
    assert published["published"] is True
    assert published["event"]["created_at"] > 1_700_000_100


def test_a_version_that_does_not_verify_is_never_the_newest(map_client, world):
    """[R6] Nor is it dated after, or followed to the relays it names: the good
    version is the newest, and the relays without it get it."""
    says_it_all = [[key, world.hex, PUBLIC_TA] for key in FIVE]
    good = _version(world.assistant, says_it_all, 1_700_000_000)
    forged = dict(
        _version(world.assistant, [], 1_700_000_100),
        tags=[["30382:rank", world.hex, "wss://forged.example"]],
    )
    world.held[INTERNAL_TA] = [good]
    world.held[OUTBOX[0]] = [forged]

    published = _post(map_client, True)
    assert published["event"] == good
    assert {r["url"] for r in published["relays"]} == set(OUTBOX)
    assert all("wss://forged.example" not in urls for urls, _ in world.sent)


def test_nothing_to_change_publishes_nothing_and_remembers_the_choice(
    map_client, world
):
    """[E3, E4, E7]"""
    first = _post(map_client, True)
    assert first["published"] is True

    again = _post(map_client, True)
    assert again == {"on": True, "published": False, "event": None, "relays": []}
    assert len(world.sent) == 1
    assert world.choices == [True, True]

    world.held.clear()
    assert _post(map_client, False)["published"] is False


def test_nothing_to_change_still_sends_the_version_where_it_is_missing(
    map_client, world
):
    """A press whose TA relay was down left that relay without the version.
    The next press finds nothing to change, and sends it the same version."""
    world.down.add(INTERNAL_TA)
    first = _post(map_client, True)
    assert {r["url"]: r["ok"] for r in first["relays"]}[PUBLIC_TA] is False
    world.down.clear()

    again = _post(map_client, True)

    assert again["published"] is True and again["event"] == first["event"]
    assert again["relays"] == [{"url": PUBLIC_TA, "ok": True, "message": None}]
    assert world.held[INTERNAL_TA][-1] == first["event"]
    assert _post(map_client, True)["published"] is False


def test_a_relay_holding_an_older_version_is_sent_the_newest(map_client, world):
    """Not only a relay with none: one that answered with an earlier version,
    such as the one before a press it missed, gets the newest too."""
    says_it_all = [[key, world.hex, PUBLIC_TA] for key in FIVE]
    latest = _version(world.assistant, says_it_all, 1_700_000_100)
    world.held[INTERNAL_TA] = [_version(world.assistant, [], 1_700_000_000)]
    world.held[OUTBOX[0]] = [latest]
    world.held[OUTBOX[1]] = [latest]

    again = _post(map_client, True)

    assert again["published"] is True and again["event"] == latest
    assert again["relays"] == [{"url": PUBLIC_TA, "ok": True, "message": None}]
    assert world.held[INTERNAL_TA][-1] == latest


def test_a_repair_no_relay_takes_still_answers_published(map_client, world):
    """By design: the version is already on the relay it was read from, so the
    answer is the report, its failed lines included, not a 502."""
    _post(map_client, True)
    world.held[INTERNAL_TA].clear()
    world.refusing[INTERNAL_TA] = "blocked: read-only"

    again = _post(map_client, True)

    assert again["published"] is True
    assert again["relays"] == [
        {"url": PUBLIC_TA, "ok": False, "message": "blocked: read-only"}
    ]
    assert world.choices == [True, True]


def test_a_relay_that_does_not_answer_is_not_sent_the_version_again(map_client, world):
    """It is likely down: sent to on every press, it would be reported on every
    press that has nothing to change."""
    _post(map_client, True)
    world.held.pop(OUTBOX[1])
    world.silent.add(OUTBOX[1])

    assert _post(map_client, True)["published"] is False
    assert len(world.sent) == 1


def test_turn_off_lists_none_of_its_duties(map_client, world):
    """[E6]"""
    tags = [[k, world.hex, PUBLIC_TA] for k in FIVE]
    world.held[INTERNAL_TA] = [_version(world.assistant, tags, 1_700_000_000)]

    published = _post(map_client, False)
    event = published["event"]

    assert published["on"] is False and published["published"] is True
    assert event["tags"] == [["client", "Brainstorm"]]
    assert event["content"] == ""
    assert world.choices == [False]


def test_the_choice_is_saved_after_the_publish_and_committed_before_the_answer(
    map_client, world
):
    """Saved before the publish, the caller's row stayed locked through the
    sign, connect and send; and `get_db` commits only after the response."""
    _post(map_client, True)
    assert world.log == ["sent", "saved", "committed"]

    world.log.clear()
    _post(map_client, True)
    assert world.log == ["saved", "committed"]

    # A repair, when the TA relay has lost the version, sends first too.
    world.held[INTERNAL_TA].clear()
    world.log.clear()
    assert _post(map_client, True)["published"] is True
    assert world.log == ["sent", "saved", "committed"]


def test_a_press_that_errors_saves_nothing(map_client, world):
    world.down.update([INTERNAL_TA, *OUTBOX])
    r = map_client.post("/user/assistantTreasureMap", json={"on": True})
    assert r.status_code == 502
    assert world.log == [] and world.choices == []


def test_turn_on_with_nothing_live_takes_the_stale_entries_off(map_client, world):
    """[E5] Turn on publishes what the Map should say when that differs, and
    that can be nothing."""
    world.row.last_time_published_graperank = None
    tags = [[k, world.hex, PUBLIC_TA] for k in FIVE]
    world.held[INTERNAL_TA] = [_version(world.assistant, tags, 1_700_000_000)]

    event = _post(map_client, True)["event"]
    assert event["tags"] == [["client", "Brainstorm"]]


def test_when_no_relay_answers_nothing_is_published_or_remembered(map_client, world):
    """[R3] An unread version could be dated ahead of now, or name relays the
    press would then miss."""
    world.silent.update([INTERNAL_TA, *OUTBOX, DICTIONARY])

    r = map_client.post("/user/assistantTreasureMap", json={"on": True})

    assert r.status_code == 502
    assert (
        r.json()["detail"]
        == "Couldn't read your Assistant's Treasure Map, so nothing was published."
    )
    assert world.sent == [] and world.choices == []


def test_one_relay_answering_with_none_is_a_finished_read(map_client, world):
    """[R4] The self-entries alone, with empty content."""
    world.silent.update([INTERNAL_TA, OUTBOX[1], DICTIONARY])

    event = _post(map_client, True)["event"]
    assert _self_entries(event, world.hex) == _five()
    assert event["content"] == ""


def test_turn_on_needs_the_callers_map(map_client, world):
    """[R1]"""
    world.map_unreadable = True
    r = map_client.post("/user/assistantTreasureMap", json={"on": True})
    assert r.status_code == 502
    assert world.sent == [] and world.choices == []


def test_turn_off_needs_only_the_assistants_map(map_client, world):
    """[R8]"""
    world.held[INTERNAL_TA] = [
        _version(world.assistant, [["30382:rank", world.hex, PUBLIC_TA]], 1_700_000_000)
    ]
    world.map_unreadable = True

    event = _post(map_client, False)["event"]
    assert _self_entries(event, world.hex) == set()


def test_it_is_sent_to_the_relays_and_every_relay_an_earlier_version_named(
    map_client, world
):
    """[W2, W3] § 9.2 "Finding a self-Map": the TA relay at its internal address."""
    world.held[OUTBOX[0]] = [
        _version(
            world.assistant,
            [["30382:rank", world.hex, "wss://old-scores.example"]],
            1_700_000_000,
        )
    ]

    _post(map_client, True)

    ((urls, _),) = world.sent
    assert INTERNAL_TA in urls and PUBLIC_TA not in urls
    assert {*OUTBOX, "wss://old-scores.example"} <= set(urls)
    assert len(urls) == len(set(urls))
    assert set(world.read_from) == {INTERNAL_TA, *OUTBOX}


def test_a_press_never_connects_where_the_callers_map_points(map_client, world):
    """The caller writes their own Map, so a hint for the Assistant can name any
    host. The GET lists the hints for readers; a press reads and sends only
    where § 9.2 sends a writer. The Dictionary relay, a hint from `/setup`'s
    Concept rows, refuses a kind 10040 anyway."""
    internal = "ws://10.0.0.5:6379"
    world.reader_map["tags"] = [
        *designation_rows(world.hex),
        ["30382:rank", world.hex, internal],
    ]
    assert {internal, DICTIONARY} <= set(_get(map_client)["relays"])

    _post(map_client, True)

    assert {internal, DICTIONARY}.isdisjoint(world.read_from)
    ((urls, _),) = world.sent
    assert {internal, DICTIONARY}.isdisjoint(urls)


def test_the_report_names_public_addresses_one_line_per_relay(map_client, world):
    world.down.add(OUTBOX[1])
    world.refusing[OUTBOX[0]] = "blocked: rate-limited"

    report = {r["url"]: r for r in _post(map_client, True)["relays"]}

    assert set(report) == {PUBLIC_TA, *OUTBOX}
    assert report[PUBLIC_TA]["ok"] is True
    # The transport's own error is logged, not answered.
    assert report[OUTBOX[1]] == {
        "url": OUTBOX[1],
        "ok": False,
        "message": "couldn't connect",
    }
    assert report[OUTBOX[0]]["message"] == "blocked: rate-limited"


def test_a_version_no_relay_took_is_an_error(map_client, world):
    world.down.update([INTERNAL_TA, *OUTBOX, DICTIONARY])
    r = map_client.post("/user/assistantTreasureMap", json={"on": True})
    assert r.status_code == 502
    assert isinstance(r.json()["detail"], str)


@pytest.mark.parametrize("body", [{}, {"on": "true"}, {"on": 1}, {"on": None}])
def test_the_body_is_one_boolean(map_client, world, body):
    """[P3]"""
    r = map_client.post("/user/assistantTreasureMap", json=body)
    assert r.status_code == 422
    assert world.sent == [] and world.choices == []


def test_the_request_cannot_choose_whose_assistant_or_where(map_client, world):
    """[P5]"""
    r = map_client.post(
        "/user/assistantTreasureMap",
        json={"on": True, "assistant": B, "relays": ["wss://evil.example"]},
    )
    assert r.status_code == 200
    event = r.json()["data"]["event"]
    assert event["pubkey"] == world.hex
    ((urls, _),) = world.sent
    assert "wss://evil.example" not in urls


def test_a_second_press_while_one_is_under_way_is_refused(world):
    """[L1] Two at once could sign versions with the same created_at (§ 9.2
    "When it changes"). Queued, the second would answer only after both had run,
    past the UI's 30 s, so it is refused, and the next press after goes ahead."""

    async def both():
        return await asyncio.gather(
            self_map_service.publish_assistant_treasure_map(world.db, world.user, True),
            self_map_service.publish_assistant_treasure_map(
                world.db, world.user, False
            ),
            return_exceptions=True,
        )

    first, second = asyncio.run(both())

    assert _self_entries(first.event, world.hex) == _five()
    assert isinstance(second, HTTPException) and second.status_code == 409
    assert isinstance(second.detail, str)
    assert len(world.sent) == 1
    assert self_map_service._pressing == set()

    after = asyncio.run(
        self_map_service.publish_assistant_treasure_map(world.db, world.user, False)
    )
    assert after.published and _self_entries(after.event, world.hex) == set()


def test_a_refused_press_leaves_the_running_one_its_slot(world):
    """The 409 frees nothing: a third press while the first is under way is
    refused too, and only the first publishes."""

    async def three():
        return await asyncio.gather(
            *(
                self_map_service.publish_assistant_treasure_map(
                    world.db, world.user, True
                )
                for _ in range(3)
            ),
            return_exceptions=True,
        )

    first, *others = asyncio.run(three())

    assert _self_entries(first.event, world.hex) == _five()
    assert [getattr(o, "status_code", o) for o in others] == [409, 409]
    assert len(world.sent) == 1
    assert self_map_service._pressing == set()


def test_a_version_that_fails_its_own_check_is_never_sent(
    map_client, world, monkeypatch
):
    """`sign_checked` reads the signed version back before anything is sent:
    here one short of a self-entry it should carry."""
    monkeypatch.setattr(
        self_map_service,
        "self_map_tags",
        lambda author, entries: self_map_tags(author, entries)[1:],
    )

    r = map_client.post("/user/assistantTreasureMap", json={"on": True})

    assert r.status_code == 500 and isinstance(r.json()["detail"], str)
    assert world.sent == [] and world.choices == []


def test_a_press_that_errors_lets_the_next_one_go_ahead(map_client, world):
    world.map_unreadable = True
    assert (
        map_client.post("/user/assistantTreasureMap", json={"on": True}).status_code
        == 502
    )
    world.map_unreadable = False
    assert _post(map_client, True)["published"] is True
