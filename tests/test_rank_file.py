"""The BSRK rank file: what an app downloads, saves and queries by pubkey.

The builder and the reference reader (`RankFile`) are the spec in code
(docs/rank-file-format.md), so the round trip is the contract: every key in
comes back with its Rank, every key not in comes back as a miss.
"""

import math
import os
import random
import struct

import pytest

from app.services.rank_file import (
    HEADER_SIZE,
    KEY_BYTES,
    MAGIC,
    RankFile,
    build_rank_file,
    root_bits_for,
)


def _rows(n: int, seed: int = 1) -> list[tuple[str, int]]:
    rng = random.Random(seed)
    return [(rng.randbytes(32).hex(), rng.randint(2, 100)) for _ in range(n)]


def test_every_key_round_trips_and_strangers_miss():
    rows = _rows(300_000)
    f = RankFile(build_rank_file(rows, 2))

    assert f.count == len(rows)
    assert all(f.rank(pubkey) == rank for pubkey, rank in rows[::10])
    assert all(f.rank(os.urandom(32)) is None for _ in range(5_000))


def test_lookup_takes_hex_or_raw_bytes():
    rows = _rows(100)
    f = RankFile(build_rank_file(rows, 2))
    pubkey, rank = rows[0]

    assert f.rank(pubkey) == f.rank(bytes.fromhex(pubkey)) == rank


def test_first_and_last_slot_resolve():
    # The root's trailing entries are what bound the last slot; the first slot
    # has no predecessor. Both edges must still find their keys.
    rows = [("00" * 32, 7), ("ff" * 32, 93), ("80" + "00" * 31, 50)]
    f = RankFile(build_rank_file(rows, 2))

    assert [f.rank(p) for p, _ in rows] == [7, 93, 50]
    assert f.rank("00" * 5 + "01" + "00" * 26) is None
    assert f.rank("ff" * 5 + "fe" + "ff" * 26) is None


def test_only_the_prefix_is_compared_by_design():
    # The file keeps slot bits + 80 bits of each key, not all 256: a key that
    # agrees on that prefix is a hit. Safe because reaching one on purpose
    # costs >= 2^78 work (see the growth test below).
    f = RankFile(build_rank_file([("00" * 32, 7)], 2))

    assert f.rank("00" * 31 + "01") == 7


def test_header_records_count_and_min_rank():
    data = build_rank_file(_rows(10), 40)
    f = RankFile(data)

    assert data[:4] == MAGIC
    assert f.min_rank == 40
    assert f.count == 10


def test_empty_file_is_valid_and_misses_everything():
    f = RankFile(build_rank_file([], 2))

    assert f.count == 0
    assert f.rank(os.urandom(32)) is None


def test_rejects_something_that_is_not_a_rank_file():
    with pytest.raises(ValueError):
        RankFile(b"NOPE" + bytes(60))


@pytest.mark.parametrize("count", [300_000, 1_000_000, 10_000_000])
def test_buckets_stay_short_and_records_fixed_as_the_network_grows(count):
    bits = root_bits_for(count)
    per_slot = count / (1 << bits)

    # ~2-4 records per slot at any size, so a lookup is one index + a short scan.
    assert 1 < per_slot <= 4
    # Grinding a key onto any trusted prefix costs >= 2^78 at any size: the
    # stored prefix (slot bits + 80) outgrows log2(count) by at least 78.
    assert bits + KEY_BYTES * 8 - math.log2(count) >= 78


def test_size_is_ten_bytes_of_key_one_of_rank_plus_the_root():
    rows = _rows(300_000)
    data = build_rank_file(rows, 2)
    slots = 1 << root_bits_for(len(rows))
    root = 4 * (slots // 256 + 1) + 2 * (slots + 1)

    assert len(data) == HEADER_SIZE + root + (KEY_BYTES + 1) * len(rows)
    # ~3.56 MB at 300k keys — vs ~11 MB for the gzipped JSON.
    assert len(data) < 3.6e6


def test_ranks_sit_in_their_own_column_after_the_keys():
    rows = [("ab" * 32, 42)]
    data = build_rank_file(rows, 2)

    assert data[-1] == 42
    (count,) = struct.unpack_from(">I", data, 8)
    assert count == 1
