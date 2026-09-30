"""Measure how long the event loop is blocked, and peak RSS, while a block runs."""

import asyncio
import os
import time

_STATM = "/proc/self/statm"
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _rss_mb() -> float | None:
    try:
        with open(_STATM) as f:
            return int(f.read().split()[1]) * _PAGE_SIZE / 1e6
    except (OSError, ValueError, IndexError):
        return None


class LoopProbe:
    """`async with LoopProbe() as probe:` — a timer task that wakes every
    `interval_s` and records how late it woke (time the loop was blocked)."""

    def __init__(self, interval_s: float = 0.005, rss_every: int = 20) -> None:
        self.interval_s = interval_s
        self.rss_every = rss_every
        self.lags: list[float] = []
        self.rss_peak_mb: float | None = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "LoopProbe":
        self._sample_rss()
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._task is not None
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._sample_rss()

    async def _run(self) -> None:
        while True:
            start = time.perf_counter()
            await asyncio.sleep(self.interval_s)
            self.lags.append(max(time.perf_counter() - start - self.interval_s, 0.0))
            if len(self.lags) % self.rss_every == 0:
                self._sample_rss()

    def _sample_rss(self) -> None:
        rss = _rss_mb()
        if rss is not None and (self.rss_peak_mb is None or rss > self.rss_peak_mb):
            self.rss_peak_mb = rss

    def summary(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if self.lags:
            ordered = sorted(self.lags)
            out["loop_lag_max_ms"] = round(ordered[-1] * 1e3, 1)
            out["loop_lag_p99_ms"] = round(ordered[int(len(ordered) * 0.99)] * 1e3, 1)
        if self.rss_peak_mb is not None:
            out["rss_peak_mb"] = round(self.rss_peak_mb)
        return out
