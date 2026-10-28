"""The Assistant's Dictionary: `POST /user/assistantDictionary` and
`POST /user/assistantDictionary/withdraw` (ADR assistant-dictionary/0001;
story assistant-dictionary #1).

Relays are faked at the service's import site, as the Duty Menu's tests fake
theirs: the strict reader (`fetch_newest_headers`) reads what the fake holds,
and nostr-sdk's `Client` sends into it. Signing is real, with generated keys.
The relay reader itself runs over a real socket in
`test_dictionary_relay_reader.py`.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from nostr_sdk import Event, EventBuilder, Keys, Kind, Tag, Timestamp

from app.api import app
from app.core.config import settings
from app.core.database import get_db

SERVICE = "app.services.assistant_dictionary_service"
ADD = "/user/assistantDictionary"
WITHDRAW = "/user/assistantDictionary/withdraw"
DICTIONARY = "wss://dcosl.brainstorm.world"
OUTBOX = ["wss://relay.damus.io", "wss://relay.primal.net"]
WITHDRAWN_ALT = (
    "A withdrawn concept copy: considered, and affiliated with no community concept."
)
CLIENT = ["client", "Brainstorm"]
SOMEONE_ELSES = f"39998:{'3' * 64}:github-accounts"


def signed(keys: Keys, tags: list[list[str]], created_at: int = 1_790_000_000) -> dict:
    event = (
        EventBuilder(kind=Kind(39998), content="")
        .tags([Tag.parse(t) for t in tags])
        .custom_created_at(Timestamp.from_secs(created_at))
        .sign_with_keys(keys)
    )
    return json.loads(event.as_json())


def hexkey(keys: Keys) -> str:
    return keys.public_key().to_hex()


def concept(curator: Keys, d: str = "github-accounts") -> str:
    return f"39998:{hexkey(curator)}:{d}"


def community(
    curator: Keys,
    d: str = "github-accounts",
    description: str = "A list of github handles/accounts",
    created_at: int = 1_790_000_000,
) -> dict:
    """A curator's self-pointing community header, with tags a copy leaves behind."""
    return signed(
        curator,
        [
            ["d", d],
            ["names", "GitHub Account", "GitHub Accounts"],
            ["json", "{}"],
            ["description", description],
            ["required", "github-username"],
            ["alt", "A concept"],
            ["b", concept(curator, d), "pointer"],
        ],
        created_at,
    )


def expected_copy(
    curator: Keys,
    d: str = "github-accounts",
    description: str = "A list of github handles/accounts",
) -> list[list[str]]:
    return [
        ["d", d],
        ["names", "GitHub Account", "GitHub Accounts"],
        ["description", description],
        ["required", "github-username"],
        ["b", concept(curator, d), "pointer"],
        CLIENT,
    ]


def local(assistant: Keys, d: str = "github-accounts") -> str:
    return f"39998:{hexkey(assistant)}:{d}"


class Relays:
    """The dictionary relay and the Assistant's outbox, faked as one: what they
    hold, whether the strict read fails, which refuse, and every send."""

    def __init__(self) -> None:
        self.held: list[dict] = []
        self.read_fails = False
        self.refusing: dict[str, str] = {}
        self.sent: list[tuple[list[str], dict]] = []
        self.send_delay = 0.0
        self.in_flight = 0
        self.max_in_flight = 0

    async def fetch_newest_headers(self, relay, authors, ds):
        if self.read_fails:
            raise ConnectionError("the dictionary relay didn't answer")
        newest: dict[tuple[str, str], dict] = {}
        for event in self.held:
            d = next((t[1] for t in event["tags"] if t[0] == "d"), None)
            key = (event["pubkey"], d)
            if event["kind"] != 39998 or event["pubkey"] not in authors or d not in ds:
                continue
            if key not in newest or event["created_at"] > newest[key]["created_at"]:
                newest[key] = event
        return newest

    def client(self, *_, **__):
        relays = self

        class _FakeClient:
            def __init__(self) -> None:
                self.pending: list[str] = []

            async def add_relay(self, url):
                self.pending.append(url)
                return True

            async def try_connect(self, _timeout):
                tried, self.pending = self.pending, []
                return SimpleNamespace(success=tried, failed={})

            async def send_event_to(self, urls, event):
                relays.in_flight += 1
                relays.max_in_flight = max(relays.max_in_flight, relays.in_flight)
                try:
                    if relays.send_delay:
                        await asyncio.sleep(relays.send_delay)
                    sent = json.loads(event.as_json())
                    took = [u for u in urls if u not in relays.refusing]
                    relays.sent.append((list(urls), sent))
                    if took:
                        relays.held.append(sent)
                    return SimpleNamespace(
                        success=took,
                        failed={u: m for u, m in relays.refusing.items() if u in urls},
                    )
                finally:
                    relays.in_flight -= 1

            async def disconnect(self):
                pass

        return _FakeClient()


@pytest.fixture
def assistant() -> Keys:
    return Keys.generate()


@pytest.fixture
def curator() -> Keys:
    return Keys.generate()


@pytest.fixture(autouse=True)
def relay_settings(monkeypatch):
    monkeypatch.setattr(settings, "dictionary_relay", DICTIONARY)


@pytest.fixture
def relays(monkeypatch) -> Relays:
    fake = Relays()
    monkeypatch.setattr(f"{SERVICE}.Client", fake.client)
    monkeypatch.setattr(f"{SERVICE}.fetch_newest_headers", fake.fetch_newest_headers)
    monkeypatch.setattr(f"{SERVICE}.assistant_relay_list", lambda: list(OUTBOX))
    return fake


@pytest.fixture
def assistant_key(monkeypatch, assistant) -> None:
    row = SimpleNamespace(nsec=assistant.secret_key().to_bech32())
    monkeypatch.setattr(
        f"{SERVICE}.get_or_create_brainstorm_observer_nsec_by_pubkey_on_db",
        AsyncMock(return_value=(row, False)),
    )


@pytest.fixture
def dict_client(client, relays, assistant_key):
    """The two routes, signed in, with the Assistant's key and the relays faked."""

    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[get_db] = _fake_db
    yield client
    app.dependency_overrides.pop(get_db, None)


def post(c, path: str, *concepts: str):
    return c.post(path, json={"concepts": list(concepts)})


def results(response) -> list[dict]:
    assert response.status_code == 200, response.text
    return response.json()["data"]["concepts"]


# R1 (AC1, AC9)
def test_adding_publishes_the_exact_copy_signed_by_the_assistant(
    dict_client, relays, assistant, curator
):
    relays.held.append(community(curator))
    response = post(dict_client, ADD, concept(curator))
    assert response.status_code == 200
    assert response.json()["data"]["assistant"] == hexkey(assistant)
    [result] = response.json()["data"]["concepts"]
    assert result["status"] == "published"
    assert result["community"] == concept(curator)
    assert result["local"] == local(assistant)
    event = result["event"]
    assert event["pubkey"] == hexkey(assistant)
    assert event["kind"] == 39998
    assert event["content"] == ""
    assert event["tags"] == expected_copy(curator)
    assert Event.from_json(json.dumps(event)).verify()
    assert {(line["url"], line["ok"]) for line in result["relays"]} == {
        (DICTIONARY, True),
        *((url, True) for url in OUTBOX),
    }
    assert len(relays.sent) == 1


# R2 (AC2)
def test_adding_again_with_nothing_changed_is_unchanged_and_sends_nothing(
    dict_client, relays, curator
):
    relays.held.append(community(curator))
    assert results(post(dict_client, ADD, concept(curator)))[0]["status"] == "published"
    relays.sent.clear()
    [result] = results(post(dict_client, ADD, concept(curator)))
    assert result["status"] == "unchanged"
    assert result["event"] is None
    assert result["relays"] == []
    assert relays.sent == []


# R3 (AC3)
def test_after_the_community_header_changes_adding_again_updates_the_copy(
    dict_client, relays, assistant, curator
):
    relays.held.append(community(curator))
    first = results(post(dict_client, ADD, concept(curator)))[0]["event"]
    newer = "GitHub accounts, in newer words"
    relays.held.append(community(curator, description=newer, created_at=1_790_000_100))
    [result] = results(post(dict_client, ADD, concept(curator)))
    assert result["status"] == "updated"
    assert result["local"] == local(assistant)
    assert result["event"]["tags"] == expected_copy(curator, description=newer)
    assert result["event"]["created_at"] > first["created_at"]


# R4 (AC4)
@pytest.mark.parametrize(
    "tags",
    [
        [["d", "github-accounts"], ["b", SOMEONE_ELSES, "pointer"]],
        [["d", "github-accounts"], ["names", "My own list", "My own lists"]],
    ],
)
def test_never_overwrites_a_header_that_isnt_its_copy_of_this_concept(
    dict_client, relays, assistant, curator, tags
):
    relays.held += [community(curator), signed(assistant, tags)]
    [result] = results(post(dict_client, ADD, concept(curator)))
    assert result["status"] == "conflict"
    assert isinstance(result["error"], str) and result["error"]
    assert result["event"] is None
    assert relays.sent == []


# R5 (AC4, D4)
def test_two_concepts_with_the_same_d_in_one_request_copy_only_the_first(
    dict_client, relays, curator
):
    second = Keys.generate()
    relays.held += [community(curator), community(second)]
    response = post(dict_client, ADD, concept(curator), concept(second))
    assert [r["status"] for r in results(response)] == ["published", "conflict"]
    assert len(relays.sent) == 1
    assert relays.sent[0][1]["tags"] == expected_copy(curator)


# R6 (AC5)
def test_removing_republishes_the_copy_withdrawn(
    dict_client, relays, assistant, curator
):
    relays.held.append(community(curator))
    post(dict_client, ADD, concept(curator))
    relays.sent.clear()
    [result] = results(post(dict_client, WITHDRAW, concept(curator)))
    assert result["status"] == "withdrawn"
    assert result["local"] == local(assistant)
    assert result["event"]["tags"] == [
        ["d", "github-accounts"],
        ["b", "b-tag-deferred"],
        ["alt", WITHDRAWN_ALT],
        CLIENT,
    ]
    assert result["event"]["pubkey"] == hexkey(assistant)
    assert len(relays.sent) == 1


# R7 (AC5)
def test_removing_what_isnt_there_is_unchanged(dict_client, relays, curator):
    relays.held.append(community(curator))
    [result] = results(post(dict_client, WITHDRAW, concept(curator)))
    assert result["status"] == "unchanged"
    assert relays.sent == []


# R8 (AC5)
def test_removing_never_touches_a_header_that_points_elsewhere(
    dict_client, relays, assistant, curator
):
    relays.held += [
        community(curator),
        signed(assistant, [["d", "github-accounts"], ["b", SOMEONE_ELSES, "pointer"]]),
    ]
    [result] = results(post(dict_client, WITHDRAW, concept(curator)))
    assert result["status"] == "conflict"
    assert relays.sent == []


# R9 (AC6)
def test_adding_after_removing_publishes_a_fresh_copy(dict_client, relays, curator):
    relays.held.append(community(curator))
    post(dict_client, ADD, concept(curator))
    post(dict_client, WITHDRAW, concept(curator))
    relays.sent.clear()
    [result] = results(post(dict_client, ADD, concept(curator)))
    assert result["status"] == "published"
    assert result["event"]["tags"] == expected_copy(curator)
    assert len(relays.sent) == 1


# R10 (AC7)
def test_a_concept_that_cant_be_found_is_not_found_and_nothing_is_published(
    dict_client, relays, curator
):
    [result] = results(post(dict_client, ADD, concept(curator)))
    assert result["status"] == "not_found"
    assert result["local"] is None
    assert result["event"] is None
    assert relays.sent == []


# R11 (AC8)
@pytest.mark.parametrize("path", [ADD, WITHDRAW])
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"concepts": []},
        {"concepts": f"39998:{'a' * 64}:x"},
        {"concepts": [f"39999:{'a' * 64}:x"]},
        {"concepts": ["39998:abc:x"]},
        {"concepts": [f"39998:{'a' * 64}:"]},
        {"concepts": [f"39998:{'a' * 64}:d{i}" for i in range(11)]},
    ],
)
def test_a_body_that_isnt_1_to_10_concept_coordinates_is_a_422(client, path, body):
    assert client.post(path, json=body).status_code == 422


# R12 (AC8)
def test_a_d_with_colons_is_kept_whole(dict_client, relays, assistant, curator):
    d = "lists:github:accounts"
    relays.held.append(community(curator, d=d))
    [result] = results(post(dict_client, ADD, concept(curator, d)))
    assert result["status"] == "published"
    assert result["local"] == local(assistant, d)
    assert result["event"]["tags"][0] == ["d", d]


# R13 (AC9)
@pytest.mark.parametrize("path", [ADD, WITHDRAW])
def test_both_routes_refuse_a_request_without_a_session(path):
    response = TestClient(app).post(path, json={"concepts": [f"39998:{'a' * 64}:x"]})
    assert response.status_code in (401, 403)


# R14 (AC10)
def test_the_copy_goes_to_the_dictionary_relay_and_the_outbox_dated_after_the_newest(
    dict_client, relays, assistant, curator
):
    ahead = int(time.time()) + 1000
    relays.held += [
        community(curator),
        signed(
            assistant,
            [["d", "github-accounts"], ["b", "b-tag-deferred"]],
            created_at=ahead,
        ),
    ]
    [result] = results(post(dict_client, ADD, concept(curator)))
    assert result["status"] == "published"
    assert result["event"]["created_at"] == ahead + 1
    assert set(relays.sent[0][0]) == {DICTIONARY, *OUTBOX}


# R15 (AC10)
def test_when_no_relay_takes_it_the_answer_is_502(dict_client, relays, curator):
    relays.held.append(community(curator))
    relays.refusing = {url: "blocked: not today" for url in [DICTIONARY, *OUTBOX]}
    response = post(dict_client, ADD, concept(curator))
    assert response.status_code == 502
    assert isinstance(response.json()["detail"], str)


# R16 (D6)
def test_the_relay_report_is_per_concept_and_names_a_relay_that_refused(
    dict_client, relays, curator
):
    relays.held.append(community(curator))
    relays.refusing = {OUTBOX[0]: "rate-limited: slow down"}
    [result] = results(post(dict_client, ADD, concept(curator)))
    lines = {line["url"]: line for line in result["relays"]}
    assert lines[DICTIONARY]["ok"] is True
    assert lines[OUTBOX[0]]["ok"] is False
    assert lines[OUTBOX[0]]["message"] == "rate-limited: slow down"


# R17 (D3)
@pytest.mark.parametrize("path", [ADD, WITHDRAW])
def test_when_the_dictionary_relay_cant_be_read_nothing_is_published_and_the_answer_is_502(
    dict_client, relays, curator, path
):
    relays.held.append(community(curator))
    relays.read_fails = True
    response = post(dict_client, path, concept(curator))
    assert response.status_code == 502
    assert relays.sent == []


# R18 (D4)
def test_a_concept_the_assistant_authored_itself_is_a_conflict(
    dict_client, relays, assistant
):
    mine = local(assistant, "mine")
    relays.held.append(
        signed(
            assistant,
            [["d", "mine"], ["names", "Mine", "Mine"], ["b", mine, "pointer"]],
        )
    )
    [result] = results(post(dict_client, ADD, mine))
    assert result["status"] == "conflict"
    assert relays.sent == []


# R19 (D6)
def test_one_unknown_concept_doesnt_stop_the_others(dict_client, relays, curator):
    missing = Keys.generate()
    relays.held.append(community(curator))
    response = post(dict_client, ADD, concept(curator), concept(missing, "urls"))
    assert [r["status"] for r in results(response)] == ["published", "not_found"]
    assert len(relays.sent) == 1


# R20 (D6)
def test_a_concept_not_sent_by_the_deadline_fails_out_of_time(
    monkeypatch, dict_client, relays, curator
):
    monkeypatch.setattr(f"{SERVICE}.SEND_DEADLINE_SECONDS", 0.5)
    relays.send_delay = 1.0
    other = Keys.generate()
    relays.held += [community(curator), community(other, d="urls")]
    first, second = results(
        post(dict_client, ADD, concept(curator), concept(other, "urls"))
    )
    assert first["status"] == "published"
    assert second["status"] == "failed"
    assert "out of time" in second["error"]
    assert second["event"] is None
    assert len(relays.sent) == 1


# R21 (D7)
def test_one_press_at_a_time_per_assistant(relays, assistant_key, curator):
    from app.services import assistant_dictionary_service as service

    other = Keys.generate()
    relays.held += [community(curator), community(other, d="urls")]
    relays.send_delay = 0.2
    caller = "1" * 64

    async def both():
        return await asyncio.gather(
            service.add_to_assistant_dictionary(
                MagicMock(), caller, [concept(curator)]
            ),
            service.add_to_assistant_dictionary(
                MagicMock(), caller, [concept(other, "urls")]
            ),
        )

    answers = asyncio.run(both())
    assert relays.max_in_flight == 1
    assert [a.concepts[0].status for a in answers] == ["published", "published"]


# R22 (edge: withdrawing needs only the Assistant's header)
def test_removing_a_copy_works_even_when_the_community_header_is_gone(
    dict_client, relays, assistant, curator
):
    relays.held.append(signed(assistant, expected_copy(curator)))
    [result] = results(post(dict_client, WITHDRAW, concept(curator)))
    assert result["status"] == "withdrawn"
    assert len(relays.sent) == 1
