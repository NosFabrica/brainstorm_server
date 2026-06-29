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
    calls: list[dict] | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with the NIP-50 router and external deps
    replaced by deterministic in-memory stubs.

    If ``calls`` is provided, every ``vespa_search`` invocation appends its
    kwargs to it. Sort/filter is pushed into Vespa (via ``ranking_profile`` +
    ``min_rank``), so the stub does NOT re-order or drop results — it echoes the
    canned list in order. Tests assert the router asked Vespa for the right
    profile/min_rank and preserved Vespa's ordering, not that Python re-ranked.
    """
    from app.routers.nip50 import router as nip50_module

    async def _fake_vespa_search(
        *,
        query_text: str,
        user_pubkey: str,
        hits: int,
        include_zero_score_results: bool,
        ranking_profile: str | None = None,
        min_rank: float | None = None,
    ) -> list[dict]:
        if calls is not None:
            calls.append(
                {
                    "query_text": query_text,
                    "user_pubkey": user_pubkey,
                    "hits": hits,
                    "include_zero_score_results": include_zero_score_results,
                    "ranking_profile": ranking_profile,
                    "min_rank": min_rank,
                }
            )
        # The real vespa.search() trims to `hits`; mirror that so the limit/hits
        # path is exercised faithfully (the router no longer trims in Python).
        return list(vespa_results)[:hits]

    async def _fake_fetch_kind0(authors: list[str]) -> dict[str, dict]:
        return {pk: strfry_events[pk] for pk in authors if pk in strfry_events}

    async def _noop_provision(_observer: str) -> None:
        return None

    monkeypatch.setattr(nip50_module, "vespa_search", _fake_vespa_search)
    monkeypatch.setattr(nip50_module, "_fetch_kind0_events", _fake_fetch_kind0)
    # Cold-start provisioning hits Redis + the DB; both stubbed out by
    # default. Individual tests that exercise provisioning override this.
    monkeypatch.setattr(nip50_module, "_maybe_provision_observer", _noop_provision)

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
        *,
        query_text: str,
        user_pubkey: str,
        hits: int,
        include_zero_score_results: bool,
        ranking_profile: str | None = None,
        min_rank: float | None = None,
    ) -> list[dict]:
        captured["query"] = query_text
        captured["observer"] = user_pubkey
        return [{"pubkey": pk}]

    async def _fake_fetch_kind0(authors: list[str]) -> dict[str, dict]:
        return {pk: _kind0_event(pk, "alice")}

    async def _noop_provision(_observer: str) -> None:
        return None

    monkeypatch.setattr(nip50_module, "vespa_search", _capturing_search)
    monkeypatch.setattr(nip50_module, "_fetch_kind0_events", _fake_fetch_kind0)
    monkeypatch.setattr(nip50_module, "_maybe_provision_observer", _noop_provision)

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


def test_search_limit_truncates_emitted_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``limit`` in the NIP-01 filter caps the number of EVENT frames the
    relay emits, even when the underlying search backend returns more.
    """
    pks = [chr(ord("a") + i) * 64 for i in range(5)]
    app = _build_nip50_app(
        vespa_results=[{"pubkey": pk} for pk in pks],
        strfry_events={pk: _kind0_event(pk, f"u{i}") for i, pk in enumerate(pks)},
        monkeypatch=monkeypatch,
    )
    client = TestClient(app)

    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(["REQ", "sub5", {"kinds": [0], "search": "u", "limit": 2}])
        )
        frames = _drain_until_eose(ws, "sub5")

    event_frames = [f for f in frames if f[0] == "EVENT"]
    assert len(event_frames) == 2
    # Order preserved from Vespa rank order (stub returns pks in order).
    assert [f[2]["pubkey"] for f in event_frames] == pks[:2]
    assert frames[-1] == ["EOSE", "sub5"]


# ---------------------------------------------------------------------------
# Path A extensions: sort, filter, cold-start
# ---------------------------------------------------------------------------
def test_nip11_advertises_sort_and_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_nip50_app(
        vespa_results=[], strfry_events={}, monkeypatch=monkeypatch
    )
    client = TestClient(app)
    body = client.get(
        "/relay", headers={"Accept": "application/nostr+json"}
    ).json()
    exts = body["search_capabilities"]["extensions"]
    assert "rank" in exts["sort"]["metrics"]
    assert "rank" in exts["filter"]["metrics"]
    # Only lower-bound operators map to Vespa's rank-score-drop-limit push-down.
    assert set(exts["filter"]["operators"]) == {"gte", "gt"}


def test_sort_rank_desc_selects_profile_and_preserves_vespa_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sort:rank:desc`` asks Vespa for the ``rank_desc`` profile (Vespa does
    the ordering) and the relay emits events in the order Vespa returned them.
    """
    pk_a, pk_b, pk_c = "a" * 64, "b" * 64, "c" * 64
    calls: list[dict] = []
    # Vespa returns these already ordered; the relay must not reorder them.
    app = _build_nip50_app(
        vespa_results=[{"pubkey": pk_c}, {"pubkey": pk_b}, {"pubkey": pk_a}],
        strfry_events={
            pk_a: _kind0_event(pk_a, "a"),
            pk_b: _kind0_event(pk_b, "b"),
            pk_c: _kind0_event(pk_c, "c"),
        },
        monkeypatch=monkeypatch,
        calls=calls,
    )
    client = TestClient(app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(["REQ", "s", {"kinds": [0], "search": "x sort:rank:desc"}])
        )
        frames = _drain_until_eose(ws, "s")

    assert calls[-1]["ranking_profile"] == "rank_desc"
    assert calls[-1]["min_rank"] is None
    event_pks = [f[2]["pubkey"] for f in frames if f[0] == "EVENT"]
    assert event_pks == [pk_c, pk_b, pk_a]


def test_sort_rank_asc_selects_profile_and_includes_zero_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sort:rank:asc`` uses the ``rank_asc`` profile and — unlike every other
    path — keeps zero-score hits, since those are exactly what asc surfaces.
    """
    pk_low, pk_high = "a" * 64, "b" * 64
    calls: list[dict] = []
    app = _build_nip50_app(
        vespa_results=[{"pubkey": pk_low}, {"pubkey": pk_high}],
        strfry_events={
            pk_low: _kind0_event(pk_low, "low"),
            pk_high: _kind0_event(pk_high, "high"),
        },
        monkeypatch=monkeypatch,
        calls=calls,
    )
    client = TestClient(app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(["REQ", "s", {"kinds": [0], "search": "x sort:rank:asc"}])
        )
        frames = _drain_until_eose(ws, "s")

    assert calls[-1]["ranking_profile"] == "rank_asc"
    assert calls[-1]["include_zero_score_results"] is True
    event_pks = [f[2]["pubkey"] for f in frames if f[0] == "EVENT"]
    assert event_pks == [pk_low, pk_high]


def test_filter_rank_gte_pushes_min_rank_to_vespa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``filter:rank:gte:50`` selects the filtered profile and pushes the
    threshold down as ``min_rank`` (Vespa drops sub-threshold hits, not Python).
    """
    calls: list[dict] = []
    app = _build_nip50_app(
        vespa_results=[], strfry_events={}, monkeypatch=monkeypatch, calls=calls
    )
    client = TestClient(app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(
                ["REQ", "s", {"kinds": [0], "search": "x filter:rank:gte:50"}]
            )
        )
        _drain_until_eose(ws, "s")

    assert calls[-1]["ranking_profile"] == "rank_filtered"
    assert calls[-1]["min_rank"] == 50.0


def test_filter_rank_gt_uses_strict_lower_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``gt`` is pushed down as a ``min_rank`` just above the value, so a hit
    scoring exactly the value is excluded."""
    calls: list[dict] = []
    app = _build_nip50_app(
        vespa_results=[], strfry_events={}, monkeypatch=monkeypatch, calls=calls
    )
    client = TestClient(app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(["REQ", "s", {"kinds": [0], "search": "x filter:rank:gt:50"}])
        )
        _drain_until_eose(ws, "s")

    assert calls[-1]["ranking_profile"] == "rank_filtered"
    assert 50.0 < calls[-1]["min_rank"] < 51.0


def test_filter_and_sort_compose_into_profile_and_min_rank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sort picks the profile; a filter sets min_rank — both reach Vespa."""
    calls: list[dict] = []
    app = _build_nip50_app(
        vespa_results=[], strfry_events={}, monkeypatch=monkeypatch, calls=calls
    )
    client = TestClient(app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(
                [
                    "REQ",
                    "s",
                    {"kinds": [0], "search": "x filter:rank:gte:40 sort:rank:desc"},
                ]
            )
        )
        _drain_until_eose(ws, "s")

    assert calls[-1]["ranking_profile"] == "rank_desc"
    assert calls[-1]["min_rank"] == 40.0


def test_multiple_filters_take_most_restrictive_lower_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AND-ed filter tokens collapse to the highest lower bound."""
    calls: list[dict] = []
    app = _build_nip50_app(
        vespa_results=[], strfry_events={}, monkeypatch=monkeypatch, calls=calls
    )
    client = TestClient(app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(
                [
                    "REQ",
                    "s",
                    {"kinds": [0], "search": "x filter:rank:gte:30 filter:rank:gte:70"},
                ]
            )
        )
        _drain_until_eose(ws, "s")

    assert calls[-1]["min_rank"] == 70.0


def test_unknown_sort_metric_emits_notice_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported ``sort:`` metric must not silently change behavior —
    the relay NOTICEs the client and uses the default profile (no override).
    """
    pk_a, pk_b = "a" * 64, "b" * 64
    calls: list[dict] = []
    app = _build_nip50_app(
        vespa_results=[{"pubkey": pk_a}, {"pubkey": pk_b}],
        strfry_events={
            pk_a: _kind0_event(pk_a, "a"),
            pk_b: _kind0_event(pk_b, "b"),
        },
        monkeypatch=monkeypatch,
        calls=calls,
    )
    client = TestClient(app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(
                ["REQ", "s", {"kinds": [0], "search": "x sort:followers:desc"}]
            )
        )
        frames = _drain_until_eose(ws, "s")

    notice = next((f for f in frames if f[0] == "NOTICE"), None)
    assert notice is not None
    assert "followers" in notice[1].lower()
    # A rejected sort falls back to the NIP-50 default ordering, which is
    # trust-sorted-within-text = rank_desc (P0, docs/search-vs-tapestry.md §6).
    assert calls[-1]["ranking_profile"] == "rank_desc"
    event_pks = [f[2]["pubkey"] for f in frames if f[0] == "EVENT"]
    assert event_pks == [pk_a, pk_b]


def test_unsupported_filter_op_emits_notice_and_applies_no_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lte``/``lt``/``eq`` can't be pushed into Vespa's lower-bound cut-off;
    the relay NOTICEs and applies no filter rather than guessing."""
    calls: list[dict] = []
    app = _build_nip50_app(
        vespa_results=[], strfry_events={}, monkeypatch=monkeypatch, calls=calls
    )
    client = TestClient(app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(
                ["REQ", "s", {"kinds": [0], "search": "x filter:rank:lte:50"}]
            )
        )
        frames = _drain_until_eose(ws, "s")

    notice = next((f for f in frames if f[0] == "NOTICE"), None)
    assert notice is not None
    assert "lte" in notice[1].lower()
    # Unsupported op → no filter pushed (min_rank stays None); the relay still
    # uses its trust-sorted default order (rank_desc) per P0 (§6).
    assert calls[-1]["ranking_profile"] == "rank_desc"
    assert calls[-1]["min_rank"] is None


def test_cold_start_provisioning_fires_on_first_observer_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a NEW observer is supplied via ``observer:<hex>``, the relay
    must enqueue a GrapeRank job for that observer (fire-and-forget). A
    second search for the same observer must NOT re-enqueue (Redis dedup).
    """
    from app.routers.nip50 import router as nip50_module

    pk = "a" * 64
    observer_pk = "d" * 64

    calls: list[str] = []

    async def _fake_vespa_search(
        *,
        query_text: str,
        user_pubkey: str,
        hits: int,
        include_zero_score_results: bool,
        ranking_profile: str | None = None,
        min_rank: float | None = None,
    ) -> list[dict]:
        return [{"pubkey": pk, "_quality_score": 0}]

    async def _fake_fetch_kind0(authors: list[str]) -> dict[str, dict]:
        return {pk: _kind0_event(pk, "alice")}

    async def _spy_provision(observer: str) -> None:
        # Record every provisioning call so the test can assert on the
        # de-duplication behavior across multiple searches.
        calls.append(observer)

    monkeypatch.setattr(nip50_module, "vespa_search", _fake_vespa_search)
    monkeypatch.setattr(nip50_module, "_fetch_kind0_events", _fake_fetch_kind0)
    monkeypatch.setattr(nip50_module, "_maybe_provision_observer", _spy_provision)

    app = FastAPI()
    app.include_router(nip50_module.router)
    client = TestClient(app)

    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(["REQ", "s1", {"search": f"alice observer:{observer_pk}"}])
        )
        _drain_until_eose(ws, "s1")
        ws.send_text(
            json.dumps(["REQ", "s2", {"search": f"bob observer:{observer_pk}"}])
        )
        _drain_until_eose(ws, "s2")

    # Both searches call the helper; the helper itself is responsible for
    # the Redis NX dedup. Here we just verify the relay fired it with the
    # caller-supplied observer (not the default observer).
    assert calls == [observer_pk, observer_pk]


def test_cold_start_not_fired_for_default_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the client omits ``observer:``, the relay falls back to the
    instance's default observer and must NOT fire cold-start provisioning
    (the default observer is provisioned by the periodic graperank cronjob).
    """
    from app.routers.nip50 import router as nip50_module

    pk = "a" * 64
    calls: list[str] = []

    async def _fake_vespa_search(
        *,
        query_text: str,
        user_pubkey: str,
        hits: int,
        include_zero_score_results: bool,
        ranking_profile: str | None = None,
        min_rank: float | None = None,
    ) -> list[dict]:
        return [{"pubkey": pk}]

    async def _fake_fetch_kind0(authors: list[str]) -> dict[str, dict]:
        return {pk: _kind0_event(pk, "alice")}

    async def _spy_provision(observer: str) -> None:
        calls.append(observer)

    monkeypatch.setattr(nip50_module, "vespa_search", _fake_vespa_search)
    monkeypatch.setattr(nip50_module, "_fetch_kind0_events", _fake_fetch_kind0)
    monkeypatch.setattr(nip50_module, "_maybe_provision_observer", _spy_provision)

    app = FastAPI()
    app.include_router(nip50_module.router)
    client = TestClient(app)

    with client.websocket_connect("/relay") as ws:
        ws.send_text(json.dumps(["REQ", "s", {"search": "alice"}]))
        _drain_until_eose(ws, "s")

    assert calls == []


def test_pure_extension_query_returns_eose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A search string containing ONLY extension tokens (no query text) is
    rejected with an immediate EOSE — there's nothing to actually search.
    """
    app = _build_nip50_app(
        vespa_results=[{"pubkey": "a" * 64}],
        strfry_events={"a" * 64: _kind0_event("a" * 64, "alice")},
        monkeypatch=monkeypatch,
    )
    client = TestClient(app)
    with client.websocket_connect("/relay") as ws:
        ws.send_text(
            json.dumps(
                [
                    "REQ",
                    "s",
                    {"search": f"observer:{'d' * 64} sort:rank:desc"},
                ]
            )
        )
        frames = _drain_until_eose(ws, "s")

    assert not [f for f in frames if f[0] == "EVENT"]
    assert frames[-1] == ["EOSE", "s"]
