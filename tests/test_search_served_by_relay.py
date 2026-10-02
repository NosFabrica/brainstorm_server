"""Search lives on the shared search relay; the server no longer serves it."""

from app.api import app


def _paths() -> set[str]:
    return {getattr(r, "path", None) for r in app.routes}


def test_text_search_endpoint_is_gone():
    assert "/search/byText" not in _paths()


def test_nip50_relay_is_gone():
    assert "/relay" not in _paths()


def test_open_ranking_search_stays():
    assert "/search/pubkeys" in _paths()
