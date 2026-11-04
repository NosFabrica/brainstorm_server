"""The replay of already-held kind-39999 events (story 2, AC17b; ADR
`trusted-lists/0002` D10). The writing run is in the integration suite."""
from __future__ import annotations

import asyncio
import io
from collections import Counter
from unittest.mock import AsyncMock

from scripts.backfill_event_taggings import read_events, replay
from tests.test_event_tagging_ingest import _header, _tagging
from app.services.tagging_parse import NOSTR_USER_TAG_Z_TAG, TAG_Z_TAG

ELEMENT = {
    "kind": 39999,
    "id": "1" * 64,
    "pubkey": "d" * 64,
    "created_at": 1,
    "tags": [["d", "espresso"], ["z", TAG_Z_TAG]],
    "content": '{"tag": {"slug": "espresso"}}',
}
PUBKEY_TAGGING = {
    "kind": 39999,
    "id": "2" * 64,
    "pubkey": "b" * 64,
    "created_at": 1,
    "tags": [["d", "x"], ["z", NOSTR_USER_TAG_Z_TAG], ["p", "3" * 64], ["e", "1" * 64]],
    "content": "",
}
FOREIGN = {"kind": 39999, "id": "4" * 64, "pubkey": "b" * 64, "tags": [], "content": ""}
EVENTS = [ELEMENT, PUBKEY_TAGGING, _header(), _tagging(), FOREIGN, {"kind": 1}]


def test_backfill_dry_run_writes_nothing_and_counts():
    handle = AsyncMock()
    counts = asyncio.run(replay(EVENTS, apply=False, handle=handle))
    handle.assert_not_awaited()
    assert counts == Counter(
        tag_elements=1, pubkey_taggings=1, headers=1, event_taggings=1, ignored=2
    )


def test_backfill_apply_hands_every_recognised_event_to_the_live_handler():
    handle = AsyncMock()
    asyncio.run(replay(EVENTS, apply=True, handle=handle))
    assert [c.args[0] for c in handle.await_args_list] == EVENTS[:4]


def test_backfill_counts_a_failed_event_and_goes_on():
    handle = AsyncMock(side_effect=[RuntimeError("db"), None, None, None])
    counts = asyncio.run(replay(EVENTS, apply=True, handle=handle))
    assert counts["failed"] == 1
    assert handle.await_count == 4


def test_backfill_skips_unparseable_lines():
    unreadable: Counter = Counter()
    lines = io.StringIO('{"kind": 39999}\n\nnot json\n[1, 2]\n{"kind": 1}\n')
    events = list(read_events(lines, unreadable))
    assert events == [{"kind": 39999}, {"kind": 1}]
    assert unreadable == Counter(unreadable=2)
