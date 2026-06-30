"""Per-segment wall-clock timing, shared by the publish run and the admin
reconcile (and anything else that wants a segment breakdown).
"""

import time
from contextlib import contextmanager


@contextmanager
def timed(timings: dict[str, float], name: str):
    """Record wall-clock seconds for a segment into `timings`. Works around an
    `await` inside the block — __exit__ runs after the awaited call resumes, and
    the `finally` records even when the block raises (so failed segments are
    still attributed)."""
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round(time.perf_counter() - start, 3)
