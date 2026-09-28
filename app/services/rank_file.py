"""The compact binary observee -> Rank file ("BSRK"), for apps to save and query.

Spec: docs/rank-file-format.md. In short, a one-level trie over the pubkey's
leading bits: the top `root_bits` bits pick a slot, the slot bounds a bucket of
~2-4 records, and each record holds the *next* 80 bits of the pubkey (the slot
bits are implicit) plus, in a separate column, its Rank.

Sizing keeps a colliding-key grind at >= 2^78 work at any network size: the
stored prefix is `root_bits + 80` bits and `root_bits >= ceil(log2 N) - 2`, so
the prefix grows with N while every record stays exactly 10 bytes.
"""

from __future__ import annotations

import math
import struct

MAGIC = b"BSRK"
VERSION = 1
KEY_BYTES = 10
KEY_BITS = KEY_BYTES * 8
MIN_ROOT_BITS = 8
MAX_ROOT_BITS = 26
BLOCK_BITS = 8  # the root's first level has one u32 per 256 slots

# magic, version, root_bits, key_bytes, min_rank, count
_HEADER = struct.Struct(">4sBBBBI")
HEADER_SIZE = 16  # _HEADER + 4 reserved bytes


def root_bits_for(count: int) -> int:
    """~2-4 records per slot: enough root to keep buckets short, no more."""
    if count <= 1:
        return MIN_ROOT_BITS
    return min(MAX_ROOT_BITS, max(MIN_ROOT_BITS, math.ceil(math.log2(count)) - 2))


def _split(pubkey: bytes, root_bits: int) -> tuple[int, bytes]:
    """(slot, the 80 bits after the slot bits) of a 32-byte pubkey."""
    top = int.from_bytes(pubkey[:16], "big")
    slot = top >> (128 - root_bits)
    rest = (top >> (128 - root_bits - KEY_BITS)) & ((1 << KEY_BITS) - 1)
    return slot, rest.to_bytes(KEY_BYTES, "big")


def build_rank_file(ranks: list[tuple[str, int]], min_rank: int) -> bytes:
    """Serialize `(lowercase hex pubkey, rank)` pairs, already filtered to
    `>= min_rank`. CPU-bound (~0.3s per 300k keys): run it off the event loop."""
    count = len(ranks)
    root_bits = root_bits_for(count)
    slots = 1 << root_bits

    # Lowercase hex sorts like the bytes it encodes, so sorting the strings
    # orders records by (slot, rest) with one int parse per key.
    ordered = sorted(ranks)
    rest_shift = 128 - root_bits - KEY_BITS
    rest_mask = (1 << KEY_BITS) - 1
    tops = [int(pubkey[:32], 16) for pubkey, _ in ordered]

    # Two-level root: block[k] = first record of slots [k*256, (k+1)*256), and
    # slot[i] = first record of slot i relative to its block. One trailing
    # entry each, so slot `slots` resolves to `count` without a special case.
    starts = [0] * (slots + 1)
    for top in tops:
        starts[(top >> (128 - root_bits)) + 1] += 1
    for i in range(slots):
        starts[i + 1] += starts[i]
    blocks = [starts[k << BLOCK_BITS] for k in range((slots >> BLOCK_BITS) + 1)]
    relative = [starts[i] - blocks[i >> BLOCK_BITS] for i in range(slots + 1)]

    header = _HEADER.pack(MAGIC, VERSION, root_bits, KEY_BYTES, min_rank, count)
    return b"".join(
        (
            header.ljust(HEADER_SIZE, b"\0"),
            struct.pack(f">{len(blocks)}I", *blocks),
            struct.pack(f">{len(relative)}H", *relative),
            b"".join(
                ((top >> rest_shift) & rest_mask).to_bytes(KEY_BYTES, "big")
                for top in tops
            ),
            bytes(rank for _, rank in ordered),
        )
    )


class RankFile:
    """Reference reader — the lookup a consuming app implements (see the spec)."""

    def __init__(self, data: bytes) -> None:
        magic, version, root_bits, key_bytes, min_rank, count = _HEADER.unpack_from(
            data
        )
        if magic != MAGIC or version != VERSION or key_bytes != KEY_BYTES:
            raise ValueError("not a BSRK v1 rank file")
        self.root_bits = root_bits
        self.min_rank = min_rank
        self.count = count
        slots = 1 << root_bits
        self._data = data
        self._blocks_at = HEADER_SIZE
        self._slots_at = self._blocks_at + 4 * ((slots >> BLOCK_BITS) + 1)
        self._keys_at = self._slots_at + 2 * (slots + 1)
        self._ranks_at = self._keys_at + KEY_BYTES * count

    def _start(self, slot: int) -> int:
        (block,) = struct.unpack_from(
            ">I", self._data, self._blocks_at + 4 * (slot >> BLOCK_BITS)
        )
        (offset,) = struct.unpack_from(">H", self._data, self._slots_at + 2 * slot)
        return block + offset

    def rank(self, pubkey: str | bytes) -> int | None:
        """The pubkey's Rank, or None if it is not in the file."""
        key = bytes.fromhex(pubkey) if isinstance(pubkey, str) else pubkey
        slot, rest = _split(key, self.root_bits)
        for j in range(self._start(slot), self._start(slot + 1)):
            at = self._keys_at + KEY_BYTES * j
            if self._data[at : at + KEY_BYTES] == rest:
                return self._data[self._ranks_at + j]
        return None
