"""Every function in `user_repo` that reads a score property either answers from
the Scorecard database when SCORES_IN_POSTGRES is on, or is listed here as not
converted yet. A new score read fails this until it does one or the other."""
import ast
import inspect

from app.repos import user_repo

# Shrinks to nothing as the remaining reads are converted.
NOT_CONVERTED_YET = {
    "get_paginated_section_connections",
    "get_paginated_flagged_connections",
    "get_all_section_stats",
    "get_user_graph_data",
    "get_ta_observees_for_observer",
    "get_top_inbound_by_influence",
    "get_network_alert_candidates",
    "count_above_cutoff_followers_capped",
    "count_verified_muters",
}

# The writer's side and the compromised clear: switched by their callers.
WRITE_SIDE = {
    "set_observer_scores",
    "clear_observer_scores",
    "get_pubkeys_scored_by_observer",
    "get_score_keys_on_node",
}

SCORE_READ_MARKERS = ("influence_key", "property_name", "_bind_score", "_expand(")


def _functions() -> dict[str, str]:
    tree = ast.parse(inspect.getsource(user_repo))
    source = inspect.getsource(user_repo)
    return {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _reads_scores(body: str) -> bool:
    return any(marker in body for marker in SCORE_READ_MARKERS)


def test_every_score_read_is_switched_or_listed():
    unswitched = {
        name
        for name, body in _functions().items()
        if _reads_scores(body)
        and "settings.scores_in_postgres" not in body
        and not name.endswith("_from_scorecards")
    }
    assert unswitched - WRITE_SIDE == NOT_CONVERTED_YET


def test_the_scorecard_paths_read_no_score_property():
    for name, body in _functions().items():
        if name.endswith("_from_scorecards"):
            assert not _reads_scores(body), name
