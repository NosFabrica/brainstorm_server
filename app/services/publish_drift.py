"""Published-state-drift repair decisions (pure).

Publish is delta-by-default per sink (`vespa_full_sync` / `relay_full_sync`).
Delta can drift and won't self-repair, so these helpers decide when to force a
full re-assert: a per-run, per-sink `force_full_*` override and an every-Nth
scheduled backstop. All pure — DB columns, the consumer, the scheduled trigger,
and the admin resync endpoint wire these in.
"""


def resolve_full_sync(setting_full: bool, force_full: bool | None) -> bool:
    """Whether a sink runs full this run: the env default OR a per-run override.
    `force_full` is nullable (the DB column) — None means "no override"."""
    return bool(setting_full) or bool(force_full)


def backstop_due(runs_since_full: int, every_n: int) -> bool:
    """Whether the upcoming scheduled run is the every-Nth full backstop.
    `runs_since_full` counts completed scheduled deltas since the last full, so
    the upcoming run is the (runs_since_full + 1)th. `every_n <= 0` disables it."""
    return every_n > 0 and runs_since_full + 1 >= every_n


# Admin resync target → (force_full_relay, force_full_vespa).
_RESYNC_TARGETS: dict[str, tuple[bool, bool]] = {
    "relay": (True, False),
    "vespa": (False, True),
    "both": (True, True),
}


def resync_target_to_flags(target: str) -> tuple[bool, bool]:
    """Map an admin resync `target` to per-sink force-full flags.
    Raises `ValueError` on an unknown target (the router maps it to a 4xx)."""
    try:
        return _RESYNC_TARGETS[target]
    except KeyError:
        raise ValueError(
            f"invalid resync target {target!r}; expected one of "
            f"{sorted(_RESYNC_TARGETS)}"
        )
