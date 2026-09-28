"""Serves the BSRK rank file (`rank_file.py`) for `/whitelisted/{pubkey}/ranks.bin`.

A build is CPU-bound (~0.6s at 300k keys, ~2s at 1M), so it never runs per
request: each file is built once per (observer, minRank, whitelist snapshot),
in a worker thread, and kept in a small per-process LRU. A new GrapeRank run
moves the snapshot's `updated_at`, which is part of the key, so stale files age
out rather than being invalidated.
"""

import asyncio
from collections import OrderedDict
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.repos.observer_whitelist_repo import select_whitelisted_rank_rows_of_observer
from app.services.rank_file import build_rank_file

# ~12 MB per file at 1M keys; a handful covers the observers apps actually poll.
_CACHE_SIZE = 8
_cache: OrderedDict[tuple[str, int, datetime], bytes] = OrderedDict()
# One build at a time: the burst of polls right after a run builds the file
# once and the rest read it from the cache.
_build_lock = asyncio.Lock()


async def get_rank_file_of_observer(
    db: AsyncDBSession, observer_pubkey: str, min_rank: int, snapshot_at: datetime
) -> bytes:
    key = (observer_pubkey, min_rank, snapshot_at)
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        return cached

    async with _build_lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
        rows = await select_whitelisted_rank_rows_of_observer(
            db, observer_pubkey, min_rank
        )
        data = await asyncio.to_thread(build_rank_file, rows, min_rank)
        _cache[key] = data
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
        return data


def empty_rank_file(min_rank: int) -> bytes:
    """A valid zero-record file, for an observer with no snapshot yet."""
    return build_rank_file([], min_rank)
