"""`scripts/zero_compromised_ta.py`: the rank-0 re-sign of a compromised
account's TAs. Pure parts only — no DB, no relay."""

import asyncio
import json

import pytest
from nostr_sdk import Event, Keys

from app.message_queue_tasks.ta_signing import TA_KIND
from app.repos.brainstorm_nsec import _pack_pubkeys
from scripts.zero_compromised_ta import (
    BadEntry,
    Compromised,
    Observer,
    compromised_targets,
    holds_pubkey,
    select_observers,
    sign_zero_tas,
)

NPUB = "npub18ams6ewn5aj2n3wt2qawzglx9mr4nzksxhvrdc4gzrecw7n5tvjqctp424"
HEX = "3f770d65d3a764a9c5cb503ae123e62ec7598ad035d836e2a810f3877a745b24"
PROOF = "ab" * 32


def test_targets_decode_npub_and_keep_proof():
    assert compromised_targets({NPUB: PROOF}) == [Compromised(HEX, PROOF)]
    assert compromised_targets({HEX: f" {PROOF} "}) == [Compromised(HEX, PROOF)]


@pytest.mark.parametrize("proof", ["", "  ", "not-an-id"])
def test_entry_without_a_valid_proof_is_refused(proof):
    with pytest.raises(BadEntry):
        compromised_targets({NPUB: proof})


def test_bad_pubkey_is_refused():
    with pytest.raises(BadEntry):
        compromised_targets({"npub1nope": PROOF})


def test_holds_pubkey_only_at_an_entry_boundary():
    other = "11" * 32
    assert holds_pubkey(_pack_pubkeys([other, HEX]), HEX)
    assert not holds_pubkey(_pack_pubkeys([other]), HEX)
    assert not holds_pubkey(None, HEX)
    # The target's bytes straddling two entries is not a listing of it.
    straddle = _pack_pubkeys(["00" * 16 + HEX[:32], HEX[32:] + "00" * 16])
    assert bytes.fromhex(HEX) in straddle
    assert not holds_pubkey(straddle, HEX)
    # ...but a real entry after a straddling match still counts.
    assert holds_pubkey(straddle + bytes.fromhex(HEX), HEX)


def _observer(n: int) -> Observer:
    keys = Keys.generate()
    return Observer(
        f"{n:064x}", keys.public_key().to_hex(), keys.secret_key().to_bech32()
    )


def test_select_unions_db_and_relay():
    a, b, c, d = (_observer(i) for i in range(4))
    got = select_observers(
        [a, b, c, d],
        published_by={a.pubkey, b.pubkey},
        relay_signers={b.signer, c.signer},
    )
    assert got == [(a, "db"), (b, "both"), (c, "relay")]


def test_zero_ta_is_signed_by_the_observer_at_rank_zero():
    o = _observer(1)

    async def collect():
        return [e async for e in sign_zero_tas([(o, Compromised(HEX, PROOF))])]

    (signed,) = asyncio.run(collect())
    event = Event.from_json(signed.json)
    assert event.verify()
    assert event.author().to_hex() == o.signer
    assert event.kind().as_u16() == TA_KIND
    tags = {t[0]: t[1] for t in json.loads(signed.json)["tags"]}
    assert tags["d"] == HEX
    assert tags["rank"] == "0"
    assert tags["followers"] == tags["reporters"] == tags["muters"] == "0"
    assert "hops" not in tags
