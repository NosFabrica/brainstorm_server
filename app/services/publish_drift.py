"""Published-state-drift repair decisions (pure).

Publish is delta-by-default (`relay_full_sync`). Delta can drift and won't
self-repair, so these helpers decide when to force a full re-assert: a per-run,
per-sink `force_full_*` override and an every-Nth scheduled backstop. All pure — DB
columns, the consumer, the scheduled trigger, and the admin resync endpoint wire
these in.
"""

from typing import Literal


def resolve_full_sync(setting_full: bool, force_full: bool | None) -> bool:
    """Whether the relay runs full this run: the env default OR a per-run override.
    `force_full` is nullable (the DB column) — None means "no override"."""
    return bool(setting_full) or bool(force_full)


def backstop_due(runs_since_full: int, every_n: int) -> bool:
    """Whether the upcoming scheduled run is the every-Nth full backstop.
    `runs_since_full` counts completed scheduled deltas since the last full, so
    the upcoming run is the (runs_since_full + 1)th. `every_n <= 0` disables it."""
    return every_n > 0 and runs_since_full + 1 >= every_n


ResyncTarget = Literal["relay", "neo4j", "both"]

# Admin resync target → (force_full_relay, force_full_neo4j).
RESYNC_TARGETS: dict[ResyncTarget, tuple[bool, bool]] = {
    "relay": (True, False),
    "neo4j": (False, True),
    "both": (True, True),
}
