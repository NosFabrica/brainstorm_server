"""The Observer's kind-10040 keys ride on the calc request as
`designated_pubkeys`, which the GrapeRank worker scores at a fixed 95."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.config import settings
from app.services.designation_service import parse_designated_pubkeys
from app.services.scheduler_lanes import enqueue_calc_request

OBSERVER = "a" * 64
PROVIDER = "b" * 64
OTHER_PROVIDER = "c" * 64


def test_reads_the_provider_from_each_designation_row():
    tags = [
        ["30382:rank", PROVIDER, "wss://nip85.brainstorm.world"],
        ["30382:followers", PROVIDER, "wss://nip85.brainstorm.world"],
        ["30392", OTHER_PROVIDER, "wss://relay.example"],
    ]
    assert parse_designated_pubkeys(tags, OBSERVER) == [PROVIDER, OTHER_PROVIDER]


def test_ignores_non_designation_tags_and_malformed_pubkeys():
    tags = [
        ["p", PROVIDER],
        ["30382:rank", "npub1notahexkey"],
        ["30382:rank"],
        ["30382:rank", "d" * 63],
    ]
    assert parse_designated_pubkeys(tags, OBSERVER) == []


def test_never_designates_the_observer():
    assert parse_designated_pubkeys([["30382:rank", OBSERVER]], OBSERVER) == []


def test_normalises_uppercase_hex():
    assert parse_designated_pubkeys([["30382:rank", PROVIDER.upper()]], OBSERVER) == [
        PROVIDER
    ]


def _enqueue(monkeypatch, designated):
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    redis = SimpleNamespace(rpush=AsyncMock())
    monkeypatch.setattr("app.services.scheduler_lanes.redis_client", redis)
    instance = SimpleNamespace(
        model_dump=lambda mode=None: {"private_id": 7, "parameters": OBSERVER},
        model_dump_json=lambda: json.dumps({"private_id": 7, "parameters": OBSERVER}),
    )
    asyncio.run(
        enqueue_calc_request(AsyncMock(), instance, OBSERVER, "manual", designated)
    )
    return json.loads(redis.rpush.await_args.args[1])


def test_enqueue_carries_the_designated_pubkeys(monkeypatch):
    message = _enqueue(monkeypatch, [PROVIDER])
    assert message["designated_pubkeys"] == [PROVIDER]
    assert message["parameters"] == OBSERVER


def test_enqueue_without_designation_is_unchanged(monkeypatch):
    assert "designated_pubkeys" not in _enqueue(monkeypatch, [])


def test_reads_only_our_relay(monkeypatch):
    from app.services import designation_service

    asked = []

    async def fake_fetch(relay, observer):
        asked.append(relay)
        return None

    monkeypatch.setattr(designation_service, "_fetch_latest_designation", fake_fetch)
    assert asyncio.run(designation_service.fetch_designated_pubkeys(OBSERVER)) == []
    assert asked == [settings.nostr_transfer_to_relay]


def test_a_relay_error_means_no_designation(monkeypatch):
    from app.services import designation_service

    async def boom(relay, observer):
        raise ConnectionError("down")

    monkeypatch.setattr(designation_service, "_fetch_latest_designation", boom)
    assert asyncio.run(designation_service.fetch_designated_pubkeys(OBSERVER)) == []
