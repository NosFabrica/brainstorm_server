"""Coercions for values coming back out of Neo4j.

Neo4j can hand back inf/nan for stale or unbacked influence properties and
json.dumps rejects both, so every read path that puts a graph number on the
wire runs it through here first.
"""

import math


def safe_float(value) -> float | None:
    """A JSON-safe float, or None for null / non-numeric / inf / nan."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isinf(f) or math.isnan(f):
        return None
    return f


def safe_int(value) -> int | None:
    """`safe_float` truncated to an int, so inf/nan collapse to None rather
    than raising in int()."""
    f = safe_float(value)
    return None if f is None else int(f)
