"""The public read endpoints declare no client-supplied verified threshold.

The integration tests assert a spiked `verified_threshold` doesn't *change* the
numbers, but FastAPI ignores any unknown query param, so that alone would pass
against a typo. This reads the OpenAPI schema instead, which is the actual
declared contract.

Issue: .scratch/preset-verified-counts/issues/03-preset-drive-overview-connections.md
"""

import pytest

from app.api import app

_READ_PATHS = [
    "/user/{pubkey}/overview",
    "/user/{pubkey}/stats",
    "/user/{pubkey}/connections",
]


def _query_params(path: str) -> set[str]:
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return {
                param.alias
                for param in route.dependant.query_params  # type: ignore[attr-defined]
            }
    raise AssertionError(f"no route registered for {path}")


@pytest.mark.parametrize("path", _READ_PATHS)
def test_read_endpoint_takes_no_verified_threshold(path):
    assert "verified_threshold" not in _query_params(path)


def test_connections_exposes_the_preset_driven_verified_filter():
    # The replacement for the frontend's `min_influence: getVerifiedThreshold()`
    # — the server decides which cutoff "verified" means for this section.
    assert "verified_only" in _query_params("/user/{pubkey}/connections")


@pytest.mark.parametrize("path", _READ_PATHS)
def test_read_endpoints_take_no_tier_band_overrides(path):
    # The tier bands are fixed constants (app/core/tier_thresholds.py). Letting a
    # client move them meant `/connections?tier=…` could bucket a subject
    # differently from the `/stats` count it's supposed to match — and nothing
    # ever overrode them: the frontend sent back the same 0.50/0.20/0.07.
    assert not {
        "tier_high",
        "tier_medium_high",
        "tier_medium",
    } & _query_params(path)


def test_connections_takes_no_min_influence():
    # The last client-supplied threshold. It was a `>=` stand-in for verified's
    # strict `>`, so a client could still build a "verified" list that disagreed
    # with /stats. Ticket 04 removed it once no caller passed it.
    assert "min_influence" not in _query_params("/user/{pubkey}/connections")
