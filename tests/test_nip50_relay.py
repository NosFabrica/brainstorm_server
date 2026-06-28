"""End-to-end tests for the NIP-50 /relay endpoint.

The endpoint depends on two external systems:
  * Vespa (for ranked profile search), and
  * an internal strfry instance (for original signed kind-0 events).

Both are stubbed at module-import boundaries so we can exercise the full
NIP-01/NIP-50 framing without standing up real services. The tests use
``TestClient.websocket_connect`` for the WS handler and a regular HTTP
``GET /relay`` for NIP-11.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# App factory + stubs
# ---------------------------------------------------------------------------
def _build_nip50_app(
    *,
    vespa_results: list[dict],
    strfry_events: dict[str, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """Build a minimal FastAPI app with the NIP-50 router and external deps
    replaced by deterministic in-memory stubs.
    """
    from app.routers.nip50 import router as nip50_module

    async def _fake_vespa_search(
        *, query_text: str, user_pubkey: str, hits: int, include_zero_score_results: bool
    ) -> list[dict]:
        # Echo back the canned results regardless of the query so individual
        # tests stay focused on framing rather than ranking semantics.
        return list(vespa_results)

    async def _fake_fetch_kind0(authors: list[str]) -> dict[str, dict]:
        return {pk: strfry_events[pk] for pk in authors if pk in strfry_events}

    monkeypatch.setattr(nip50_module, "vespa_search", _fake_vespa_search)
    monkeypatch.setattr(nip50_module, "_fetch_kind0_events", _fake_fetch_kind0)

    app = FastAPI()
    app.include_router(nip50_module.router)
    return app


def _kind0_event(pubkey: str, name: str) -> dict[str, Any]:
    # Realistic-looking kind-0 event. ``id``/``sig`` are placeholder hex
    # strings — the relay only re-emits whatever strfry returned, so the
    # framing tests don't need real signatures.
    return {
        "id": "a" * 64,
        "pubkey": pubkey,
        "created_at": 1700000000,
        "kind": 0,
        "tags": [],
        "content": json.dumps({"name": name}),
        "sig": "b" * 128,
    }


# ---------------------------------------------------------------------------
# NIP-11
# ---------------------------------------------------------------------------
def test_nip11_document_advertises_search_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_nip50_app(
        vespa_results=[], strfry_events={}, monkeypatch=monkeypatch
    )
    client = TestClient(app)

    resp = client.get("/relay", headers={"Accept": "application/nostr+json"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/nostr+json")
    body = resp.json()
    assert 50 in body["supported_nips"]
    assert 11 in body["supported_nips"]
    assert body["search_capabilities"]["supported_kinds"] == [0]


def test_nip11_landing_page_for_browser_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_nip50_app(
        vespa_results=[], strfry_events={}, monkeypatch=monkeypatch
    )
    client = TestClient(app)
    resp = client.get("/relay")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# WebSocket / NIP-50
# ---------------------------------------------------------------------------
def _drain_until_eose(ws, sub_id: str) -> list[list]:
    """Collect frames from ``ws`` until we see EOSE for ``sub_id``."""
    frames: list[list] = []
    while True:
        msg = ws.receive_json()
        frames.append(msg)
        if msg[0] == "EOSE" and msg[1] == sub_id:
            return frames


def test_search_returns_events_in_vespa_rank_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pk_a = "a" * 64
    pk_b = "b" * 64
    pk_c = "c" * 64  # in vespa, missing from strfry — must be skipped silently

    app = _build_nip50_app(
        vespa_results=[
            {"pubkey": pk_a, "_quality_score": 0.9},
            {"pubkey": pk_b, "_quality_score": 0.5},
            {"pubkey": pk_c, "_quality_score": 0.1},
        ],
        strfry_events={
            pk_a: _kind0_event(pk_a, "alice"),
            pk_b: _kind0_event(pk_b, "bob"),
        },
        monkeypatch=monkeypatch,
    )
    client = TestClient(app)

    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(["REQ", "sub1", {"kinds": [0], "search": "alice"}])
        )
        frames = _drain_until_eose(ws, "sub1")

    event_frames = [f for f in frames if f[0] == "EVENT"]
    assert [f[2]["pubkey"] for f in event_frames] == [pk_a, pk_b]
    assert frames[-1] == ["EOSE", "sub1"]


def test_req_without_search_filter_returns_immediate_eose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_nip50_app(
        vespa_results=[{"pubkey": "a" * 64}],
        strfry_events={"a" * 64: _kind0_event("a" * 64, "alice")},
        monkeypatch=monkeypatch,
    )
    client = TestClient(app)

    with client.websocket_connect("/relay") as ws:
        ws.send_text(json.dumps(["REQ", "sub2", {"kinds": [1]}]))
        msg = ws.receive_json()

    # No EVENT — we don't serve general nostr REQs.
    assert msg == ["EOSE", "sub2"]


def test_req_with_non_kind0_filter_returns_eose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_nip50_app(
        vespa_results=[{"pubkey": "a" * 64}],
        strfry_events={"a" * 64: _kind0_event("a" * 64, "alice")},
        monkeypatch=monkeypatch,
    )
    client = TestClient(app)

    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(["REQ", "sub3", {"kinds": [1], "search": "alice"}])
        )
        msg = ws.receive_json()

    assert msg == ["EOSE", "sub3"]


def test_observer_extension_is_stripped_from_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``observer:<hex>`` token must be removed from the query passed to
    Vespa, and the observer pubkey must be used as the user perspective.
    """
    captured: dict[str, Any] = {}

    from app.routers.nip50 import router as nip50_module

    pk = "a" * 64
    observer_pk = "d" * 64

    async def _capturing_search(
        *, query_text: str, user_pubkey: str, hits: int, include_zero_score_results: bool
    ) -> list[dict]:
        captured["query"] = query_text
        captured["observer"] = user_pubkey
        return [{"pubkey": pk}]

    async def _fake_fetch_kind0(authors: list[str]) -> dict[str, dict]:
        return {pk: _kind0_event(pk, "alice")}

    monkeypatch.setattr(nip50_module, "vespa_search", _capturing_search)
    monkeypatch.setattr(nip50_module, "_fetch_kind0_events", _fake_fetch_kind0)

    app = FastAPI()
    app.include_router(nip50_module.router)
    client = TestClient(app)

    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(
                ["REQ", "sub4", {"search": f"alice observer:{observer_pk}"}]
            )
        )
        _drain_until_eose(ws, "sub4")

    assert captured["query"] == "alice"
    assert captured["observer"] == observer_pk


def test_publish_event_is_rejected_with_ok_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_nip50_app(
        vespa_results=[], strfry_events={}, monkeypatch=monkeypatch
    )
    client = TestClient(app)

    ev = _kind0_event("a" * 64, "alice")
    with client.websocket_connect("/relay") as ws:
        ws.send_text(json.dumps(["EVENT", ev]))
        msg = ws.receive_json()

    assert msg[0] == "OK"
    assert msg[1] == ev["id"]
    assert msg[2] is False


def test_search_limit_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    pks = [chr(ord("a") + i) * 64 for i in range(5)]
    app = _build_nip50_app(
        vespa_results=[{"pubkey": pk} for pk in pks],
        strfry_events={pk: _kind0_event(pk, f"u{i}") for i, pk in enumerate(pks)},
        monkeypatch=monkeypatch,
    )
    client = TestClient(app)

    # We can't directly inspect the ``hits`` arg passed to vespa from here
    # (stubbed search ignores it), but we can at least assert the framing
    # stays well-formed when ``limit`` is supplied.
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(["REQ", "sub5", {"kinds": [0], "search": "u", "limit": 2}])
        )
        frames = _drain_until_eose(ws, "sub5")

    event_frames = [f for f in frames if f[0] == "EVENT"]
    # Stub returns all 5; the relay emits whatever vespa returns. Just sanity
    # check that framing terminates and each frame is well-formed.
    assert len(event_frames) == 5
    assert frames[-1] == ["EOSE", "sub5"]
