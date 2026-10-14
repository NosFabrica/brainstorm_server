"""Admin relay audit: the walk and dense-second handling over a fake relay, and the endpoint."""

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nostr_sdk import Keys

from app.core.database import get_db
from app.routers.admin.router import verify_admin_access
from app.services import relay_audit_service as svc

OBSERVER = "a" * 64


class FakeRelay:
    """Events as (created_at, d); honours since/until/limit/#d like strfry."""

    def __init__(self, events):
        self.events = events
        self.downloads: list[int] = []

    def _match(self, flt):
        out = self.events
        if "since" in flt:
            out = [e for e in out if flt["since"] <= e[0] <= flt["until"]]
        if "#d" in flt:
            wanted = set(flt["#d"])
            out = [e for e in out if e[1] in wanted]
        return out

    async def count(self, **flt):
        return len(self._match(flt))

    async def d_tags(self, into, **flt):
        window = sorted(self._match(flt), reverse=True)[: flt.get("limit")]
        self.downloads.append(len(window))
        into.update(d for _, d in window)


def _view(events, expected, until=10_000):
    relay = FakeRelay(events)

    async def body():
        d_tags: set[str] = set()
        dense: list[int] = []
        await svc._walk(relay, 0, until, len(events), d_tags, dense)
        present, exact = 0, True
        if dense:
            present, exact = await svc._dense_present(
                relay, sorted(set(expected) - d_tags)
            )
        return d_tags, sum(dense), present, exact

    return (*asyncio.run(body()), relay)


def test_burst_seconds_are_collected_whole(monkeypatch):
    monkeypatch.setattr(svc, "WINDOW_EVENTS", 3)
    events = [(100, f"b{i}") for i in range(5)] + [(t, f"s{t}") for t in range(1, 8)]
    d_tags, dense, _, exact, relay = _view(events, [])
    assert d_tags == {d for _, d in events} and dense == 0 and exact
    assert max(relay.downloads) <= 5


def test_windows_never_exceed_the_page_size_unless_one_second(monkeypatch):
    monkeypatch.setattr(svc, "WINDOW_EVENTS", 4)
    events = [(t, f"d{t}") for t in range(1, 50)]
    d_tags, _, _, _, relay = _view(events, [])
    assert len(d_tags) == 49 and max(relay.downloads) <= 4


def test_dense_second_is_counted_by_d_tag_not_downloaded(monkeypatch):
    monkeypatch.setattr(svc, "WINDOW_EVENTS", 2)
    monkeypatch.setattr(svc, "MAX_SECOND_EVENTS", 3)
    # second 7 holds 6 TAs: 4 expected, 2 orphans; "gone" is expected but absent
    dense = [(7, f"x{i}") for i in range(4)] + [(7, "o1"), (7, "o2")]
    expected = [f"x{i}" for i in range(4)] + ["gone"]
    d_tags, dense_events, present, exact, relay = _view(dense, expected)
    assert d_tags == set() and dense_events == 6
    assert (present, exact) == (4, True)
    assert 6 not in relay.downloads  # never fetched the dense second whole


def test_many_dense_candidates_are_sampled(monkeypatch):
    monkeypatch.setattr(svc, "WINDOW_EVENTS", 2)
    monkeypatch.setattr(svc, "MAX_SECOND_EVENTS", 3)
    monkeypatch.setattr(svc, "EXACT_DENSE_CANDIDATES", 10)
    monkeypatch.setattr(svc, "DENSE_SAMPLE", 20)
    present_d = [f"p{i}" for i in range(40)]
    events = [(7, d) for d in present_d]
    expected = present_d + [f"gone{i}" for i in range(40)]  # half missing
    _, _, present, exact, _ = _view(events, expected)
    assert exact is False
    assert 0 < present < 80  # an estimate of the 40 present


def test_sampled_estimate_never_exceeds_the_dense_seconds(monkeypatch):
    monkeypatch.setattr(svc, "WINDOW_EVENTS", 1)
    monkeypatch.setattr(svc, "MAX_SECOND_EVENTS", 1)
    monkeypatch.setattr(svc, "EXACT_DENSE_CANDIDATES", 0)
    monkeypatch.setattr(svc, "DENSE_SAMPLE", 2)
    # one dense second holds 2 TAs; 8 more expected observees are missing. A lucky
    # sample (both hits) extrapolates to 10 present — more than the second holds.
    monkeypatch.setattr(svc.random, "sample", lambda pop, k: ["a", "b"])
    expected = {"a", "b", *(f"m{i}" for i in range(8))}

    view = asyncio.run(svc._read(FakeRelay([(7, "a"), (7, "b")]), expected))

    assert (view.dense_events, view.dense_present, view.exact) == (2, 2, False)


def test_expected_set_uses_the_publishers_rounding():
    from app.repos.user_repo import get_ta_observees_for_observer

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def __aiter__(self):
            async def gen():
                for r in self.rows:
                    yield r

            return gen()

    class Session:
        async def run(self, query, **params):
            assert params["influence_key"] == f"influence_{OBSERVER}"
            rows = [("in", 0.0496), ("out", 0.0449)]
            return Result([{"pubkey": p, "influence": i} for p, i in rows])

    got = asyncio.run(get_ta_observees_for_observer(Session(), OBSERVER, 0.05))
    assert got == {"in"}


@pytest.fixture
def admin_client(client, monkeypatch):
    from app.api import app

    async def _fake_get_db():
        yield AsyncMock()

    monkeypatch.setattr(
        f"{svc.__name__}.select_brainstorm_nsec_by_pubkey_on_db",
        AsyncMock(
            return_value=SimpleNamespace(nsec=Keys.generate().secret_key().to_hex())
        ),
    )
    monkeypatch.setattr(
        f"{svc.__name__}.get_last_published_pubkeys_by_pubkey_on_db",
        AsyncMock(return_value=["p1", "p2", "p3", "old"]),
    )

    @asynccontextmanager
    async def _session():
        yield None

    monkeypatch.setattr(svc, "neo4j_driver", SimpleNamespace(session=_session))
    monkeypatch.setattr(
        f"{svc.__name__}.get_ta_observees_for_observer",
        AsyncMock(return_value={"p1", "p2", "p3", "p4"}),
    )
    app.dependency_overrides[verify_admin_access] = lambda: None
    app.dependency_overrides[get_db] = _fake_get_db
    yield client


def test_endpoint_diffs_the_relay_with_neo4j_not_last_published(
    admin_client, monkeypatch
):
    # Neo4j expects p1-p4; last_published says p1-p3 + old; the relay holds p1-p3 + old
    view = svc.RelayView(
        total=4,
        d_tags={"p1", "p2", "p3", "old"},
        dense_events=0,
        dense_present=0,
        exact=True,
    )
    monkeypatch.setattr(f"{svc.__name__}.read_relay", AsyncMock(return_value=view))

    data = admin_client.get(f"/admin/users/{OBSERVER}/relay-audit").json()["data"]

    assert (data["expected"], data["published"], data["baseline_drift"]) == (4, 4, 2)
    assert (data["missing"], data["orphans"], data["needs_resync"]) == (1, 1, True)


def test_endpoint_reports_exact_missing_and_orphans(admin_client, monkeypatch):
    view = svc.RelayView(
        total=4,
        d_tags={"p1", "p2", "p3", "stale"},
        dense_events=0,
        dense_present=0,
        exact=True,
    )
    monkeypatch.setattr(f"{svc.__name__}.read_relay", AsyncMock(return_value=view))

    response = admin_client.get(f"/admin/users/{OBSERVER}/relay-audit")

    assert response.status_code == 200
    data = response.json()["data"]
    assert Counter(
        {k: data[k] for k in ("published", "on_relay", "missing", "orphans")}
    ) == Counter(published=4, on_relay=4, missing=1, orphans=1)
    assert data["exact"] is True and data["needs_resync"] is True


def test_endpoint_folds_dense_seconds_into_missing_and_orphans(
    admin_client, monkeypatch
):
    # p1 downloaded; a dense second of 3 TAs holds p2, p3 and one orphan; p4 missing
    view = svc.RelayView(
        total=4, d_tags={"p1"}, dense_events=3, dense_present=2, exact=False
    )
    monkeypatch.setattr(f"{svc.__name__}.read_relay", AsyncMock(return_value=view))

    data = admin_client.get(f"/admin/users/{OBSERVER}/relay-audit").json()["data"]

    assert (data["missing"], data["orphans"], data["exact"]) == (1, 1, False)


def test_endpoint_502s_when_the_relay_is_unreachable(admin_client, monkeypatch):
    monkeypatch.setattr(
        f"{svc.__name__}.read_relay", AsyncMock(side_effect=OSError("down"))
    )

    response = admin_client.get(f"/admin/users/{OBSERVER}/relay-audit")

    assert response.status_code == 502
    assert "TA relay" in response.json()["detail"]
