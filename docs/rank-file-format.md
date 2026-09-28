# BSRK: the observer → Rank file

`GET /whitelisted/{observer_pubkey}/ranks.bin?minRank=N` returns every pubkey in
an observer's network at or above Rank `N` (2–100, default 2), with its Rank, as
one binary file. An app saves the bytes as-is and looks pubkeys up in place —
no parsing step, no database.

- ~11.9 bytes per key: **3.6 MB at 300k keys, 11.5 MB at 1M** (the JSON
  `/ranks` is ~11 MB gzipped at 300k).
- A lookup indexes a table with the pubkey's leading bits, then compares a
  bucket of ~2–4 records (at most ~16). No binary search.
- Public, like `/whitelisted`. `ETag` / `If-None-Match` → `304` until the
  observer's next GrapeRank run.

Reference implementations: Python `app/services/rank_file.py` (`RankFile`, the
builder's round-trip tests are the contract), JavaScript below.

## Layout (big-endian)

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | magic `"BSRK"` |
| 4 | 1 | version = `1` |
| 5 | 1 | `rootBits` (b) — the root has `2^b` slots |
| 6 | 1 | `keyBytes` = `10` |
| 7 | 1 | `minRank` the file was built with |
| 8 | 4 | `count` (N), u32 |
| 12 | 4 | reserved, zero |
| 16 | 4 × (2^b / 256 + 1) | **block table**, u32: first record index of each run of 256 slots, then `N` |
| … | 2 × (2^b + 1) | **slot table**, u16: first record index of each slot, relative to its block; last entry `0` |
| … | 10 × N | **keys**: for each record, the 80 bits of the pubkey that follow its first `b` bits |
| … | 1 × N | **ranks**: u8 Rank of each record, same order |

Records are ordered by pubkey. The first `b` bits of a pubkey are its **slot**;
they are not stored — the slot's position says them. Slot `s` holds records
`start(s) .. start(s+1)-1`, where
`start(s) = block[s >> 8] + slot[s]`.

## Lookup

1. `slot` = the pubkey's first `b` bits; `want` = its next 80 bits (10 bytes).
2. For `j` in `start(slot) .. start(slot + 1) - 1`: if `keys[j] == want`,
   return `ranks[j]`.
3. Otherwise the pubkey is not in the file: untrusted, or below `minRank`.

```js
// buf: the downloaded bytes (ArrayBuffer / Uint8Array). pubkey: 32-byte Uint8Array.
export function openRankFile(buf) {
  const u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  if (String.fromCharCode(...u8.subarray(0, 4)) !== "BSRK" || u8[4] !== 1 || u8[6] !== 10)
    throw new Error("not a BSRK v1 rank file");
  const rootBits = u8[5], minRank = u8[7], count = dv.getUint32(8);
  const slots = 2 ** rootBits;
  const blocksAt = 16;
  const slotsAt = blocksAt + 4 * (slots / 256 + 1);
  const keysAt = slotsAt + 2 * (slots + 1);
  const ranksAt = keysAt + 10 * count;
  const start = (s) => dv.getUint32(blocksAt + 4 * (s >>> 8)) + dv.getUint16(slotsAt + 2 * s);

  // pubkey: 32-byte Uint8Array. Returns the Rank, or null if not in the file.
  function rank(pubkey) {
    let top = 0n;
    for (let i = 0; i < 16; i++) top = (top << 8n) | BigInt(pubkey[i]);
    const slot = Number(top >> BigInt(128 - rootBits));
    let rest = (top >> BigInt(48 - rootBits)) & ((1n << 80n) - 1n);
    const want = new Uint8Array(10);
    for (let i = 9; i >= 0; i--) { want[i] = Number(rest & 0xffn); rest >>= 8n; }
    for (let j = start(slot), end = start(slot + 1); j < end; j++) {
      const at = keysAt + 10 * j;
      let k = 0;
      while (k < 10 && u8[at + k] === want[k]) k++;
      if (k === 10) return u8[ranksAt + j];
    }
    return null;
  }
  return { rootBits, minRank, count, rank };
}
```

## Why it is shaped like this

**Only a prefix of each pubkey is stored.** The file can only answer "is this
pubkey in the set", so it needs enough bits to tell members from everyone else,
not all 256. The risk is someone grinding keypairs until one shares a prefix
with *any* of the N trusted keys — that costs about `2^(prefix bits − log2 N)`
attempts. The prefix here is `b + 80` bits and `b ≥ ceil(log2 N) − 2`, so the
grind costs at least `2^78` at every network size, while each record stays
exactly 10 bytes. An accidental match is ~`2^-78` per lookup.

**The root grows with the network.** `b = ceil(log2 N) − 2` (min 8), giving
~2–4 records per slot: 2^17 slots at 300k keys, 2^18 at 1M, 2^22 at 10M.
Read `b` from the header; never hard-code it.

**It is a flattened trie.** A 256-way trie over the pubkey bytes has two full
levels and then almost only single-key chains (66 MB of pointers at level 3 for
300k keys). Collapsing the full levels into one direct-indexed table, and the
chains into short buckets, is the same navigation with no pointers.

**It is near the floor.** Storing N prefixes of `b + 80` bits cannot take less
than ~`N × (b + 80 − log2 N + 1.44)` bits. With the Ranks this file is within
~11–14% of that (3.56 MB vs 3.13 MB at 300k). Minimal perfect hashing or fuse
filters need the same 80 check bits per key and add a library dependency or
empty slots; they do not come out smaller.

**Ranks are a separate column** so a gzip'd transfer packs them (most keys
sit at low Ranks); the keys themselves are random and do not compress.

**`minRank` is server-side.** Only keys at or above it are written, and the
header records it, so a miss means "below `minRank` or not trusted". Each
`minRank` is a different file with a different `ETag`.

## Serving

Built once per (observer, `minRank`, GrapeRank snapshot) in a worker thread and
held in a small per-process LRU (`app/services/rank_file_service.py`): ~0.6 s
at 300k keys, ~2 s at 1M. Everything after the first download of a snapshot is
a cache hit or a `304`.
