"""
End-to-end validation for POST /admin/trustedLists/{observer_pubkey}.

Triggers Trusted List generation for one Observer and shows you exactly what
happened at every layer: the run report (dictionary size, per-tag counts,
empty_reason), and — with --verify-relay — the kind-30392 events actually
sitting on the relay afterwards, fetched by the signing key the run reported.

An empty dev box has no taggings, so --seed pushes a synthetic tag element +
tagging through the REAL ingest path (the strfry:events redis queue — not a
direct DB write) and seeds the asserter's rank in Neo4j, so the whole
pipeline is exercised, not just the endpoint.

Usage (from the repo root, in an env where app.* resolves):
  python -m scripts.test_admin_trusted_lists --observer <hex|npub>
  python -m scripts.test_admin_trusted_lists --observer <hex> --seed --verify-relay
  python -m scripts.test_admin_trusted_lists --observer <hex> --base-url http://localhost:8000

Prompts for your ADMIN nsec (must be in ADMIN_WHITELISTED_PUBKEYS and
registered as a user). The Observer is whoever you're generating lists FOR.
"""

import argparse
import asyncio
import getpass
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
from nostr_sdk import Client, EventBuilder, Filter, Keys, Kind, PublicKey, Tag

sys.path.insert(0, str(Path(__file__).parent))
from get_admin_token import fetch_admin_token  # noqa: E402


def seed(observer_hex: str) -> dict:
    """Push a signed tag element + tagging through the real redis ingest, and
    give the asserter rank in the Observer's web of trust."""
    import redis as redis_sync
    from neo4j import GraphDatabase

    from app.core.config import settings
    from app.services.tagging_parse import NOSTR_USER_TAG_Z_TAG, TAG_Z_TAG

    tag_author = Keys.generate()
    asserter = Keys.generate()
    target = Keys.generate()
    slug = f"validation-{int(time.time())}"

    element = (
        EventBuilder(
            Kind(39999),
            json.dumps({"tag": {"slug": slug, "name": slug.title(),
                                "description": "seeded by test_admin_trusted_lists"}}),
        )
        .tags([Tag.parse(["d", slug]), Tag.parse(["z", TAG_Z_TAG])])
        .sign_with_keys(tag_author)
    )
    tagging = (
        EventBuilder(Kind(39999), "")
        .tags([
            Tag.parse(["d", f"profile-tag-{slug}-x"]),
            Tag.parse(["z", NOSTR_USER_TAG_Z_TAG]),
            Tag.parse(["p", target.public_key().to_hex()]),
            Tag.parse(["e", element.id().to_hex()]),
            Tag.parse(["polarity", "1"]),
        ])
        .sign_with_keys(asserter)
    )

    r = redis_sync.Redis(host=settings.redis_host, port=int(settings.redis_port))
    r.rpush("strfry:events", element.as_json())
    r.rpush("strfry:events", tagging.as_json())

    drv = GraphDatabase.driver(
        settings.neo4j_db_url,
        auth=(settings.neo4j_db_username, settings.neo4j_db_password),
    )
    with drv.session() as s:
        s.run(
            "MERGE (u:NostrUser {pubkey: $pk}) SET u[$key] = 0.9",
            pk=asserter.public_key().to_hex(),
            key=f"influence_{observer_hex}",
        )
    drv.close()
    print(f"seeded: tag '{slug}', asserter rank 90 under this observer")
    print("  (waiting 3s for the queue consumer to ingest)")
    time.sleep(3)
    return {"slug": slug, "target": target.public_key().to_hex()}


# Ranks chosen to reproduce the D12 parity vectors live, so the published list
# demonstrates the property that motivated weighting: MASS BEATS COUNT. Ten
# rank-3 appliers must land BELOW two rank-90 appliers, and the two contested
# targets behave differently depending on where the weight sits.
WEIGHTED_SCENARIO = [
    # (label, [(rank, polarity), ...], expected score or None if excluded)
    ("two-heavy", [(90, 1), (90, 1)], 71),
    ("one-max", [(100, 1)], 50),
    ("ten-light", [(3, 1)] * 10, 19),
    ("contested-light-dispute", [(40, 1), (40, 1), (40, -1)], 19),
    ("contested-even-split", [(40, 1), (40, 1), (80, -1)], None),
    ("contested-heavy-dispute", [(3, 1), (3, 1), (90, -1)], None),
]


def seed_weighted(observer_hex: str) -> dict:
    """Seed ONE tag applied to six targets by asserters of differing rank.

    Everything rides the real ingest path (the strfry:events redis queue), same
    as `seed`. The point is the ORDER of the resulting list: if scores were a
    head-count, `ten-light` would top it; under D12 it sits third.
    """
    import redis as redis_sync
    from neo4j import GraphDatabase

    from app.core.config import settings
    from app.services.tagging_parse import NOSTR_USER_TAG_Z_TAG, TAG_Z_TAG

    tag_author = Keys.generate()
    slug = f"weighted-{int(time.time())}"

    element = (
        EventBuilder(
            Kind(39999),
            json.dumps({"tag": {"slug": slug, "name": slug.title(),
                                "description": "D12 weighted-certainty scenario"}}),
        )
        .tags([Tag.parse(["d", slug]), Tag.parse(["z", TAG_Z_TAG])])
        .sign_with_keys(tag_author)
    )

    r = redis_sync.Redis(host=settings.redis_host, port=int(settings.redis_port))
    r.rpush("strfry:events", element.as_json())

    drv = GraphDatabase.driver(
        settings.neo4j_db_url,
        auth=(settings.neo4j_db_username, settings.neo4j_db_password),
    )
    expected = {}
    with drv.session() as neo:
        for label, asserters, want in WEIGHTED_SCENARIO:
            target = Keys.generate()
            expected[target.public_key().to_hex()] = (label, want)
            for i, (rank, polarity) in enumerate(asserters):
                asserter = Keys.generate()
                tagging = (
                    EventBuilder(Kind(39999), "")
                    .tags([
                        Tag.parse(["d", f"profile-tag-{slug}-{label}-{i}"]),
                        Tag.parse(["z", NOSTR_USER_TAG_Z_TAG]),
                        Tag.parse(["p", target.public_key().to_hex()]),
                        Tag.parse(["e", element.id().to_hex()]),
                        Tag.parse(["polarity", str(polarity)]),
                    ])
                    .sign_with_keys(asserter)
                )
                r.rpush("strfry:events", tagging.as_json())
                # Rank -> Influence is the inverse of the wire quantum.
                neo.run(
                    "MERGE (u:NostrUser {pubkey: $pk}) SET u[$key] = $inf",
                    pk=asserter.public_key().to_hex(),
                    key=f"influence_{observer_hex}",
                    inf=rank / 100,
                )
    drv.close()

    total = sum(len(a) for _, a, _ in WEIGHTED_SCENARIO)
    print(f"seeded: tag '{slug}', {len(WEIGHTED_SCENARIO)} targets, {total} taggings")
    print("  expected, in published order:")
    for label, _, want in sorted(
        WEIGHTED_SCENARIO, key=lambda x: (-(x[2] or 0), x[0])
    ):
        print(f"    {label:<26} {'EXCLUDED' if want is None else want}")
    print("  (waiting 5s for the queue consumer to ingest)")
    time.sleep(5)
    return {"slug": slug, "expected": expected}


async def read_relay(signing_pubkey: str) -> list[dict]:
    from app.core.config import settings

    relay = settings.trusted_list_relay or settings.nostr_upload_ta_events_relay
    client = Client()
    await client.add_relay(relay)
    await client.connect()
    flt = (
        Filter()
        .kinds([Kind(30392)])
        .authors([PublicKey.parse(signing_pubkey)])
        .limit(200)
    )
    events = await client.fetch_events(flt, timeout=timedelta(seconds=10))
    out = [json.loads(e.as_json()) for e in events.to_vec()]
    await client.shutdown()
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate /admin/trustedLists")
    parser.add_argument("--observer", required=True, help="Observer pubkey (hex or npub)")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--seed", action="store_true",
                        help="Push a synthetic tagging through the real ingest first")
    parser.add_argument("--seed-weighted", action="store_true",
                        help="Seed the D12 scenario: one tag, six targets, "
                             "asserters of differing rank — shows mass beating count")
    parser.add_argument("--verify-relay", action="store_true",
                        help="Read the observer's kind-30392s back off the relay after")
    args = parser.parse_args()

    observer = PublicKey.parse(args.observer).to_hex()
    keys = Keys.parse(getpass.getpass("Admin nsec: "))
    print(f"Admin:    {keys.public_key().to_hex()}")
    print(f"Observer: {observer}\n")

    seeded = seed(observer) if args.seed else None
    weighted = seed_weighted(observer) if args.seed_weighted else None

    token = fetch_admin_token(args.base_url, keys)
    r = httpx.post(
        f"{args.base_url}/admin/trustedLists/{observer}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    print(f"HTTP {r.status_code}")
    body = r.json()
    data = body.get("data", body)
    print(json.dumps(data, indent=2))

    if r.status_code != 200:
        sys.exit(1)

    # Interpret the run report so failure modes read as words, not numbers.
    if data.get("empty_reason") == "no_taggings_ingested":
        print("\n!! Nothing was ever ingested — the tagging store is EMPTY.")
        print("   This is the un-synced-relay shape, not a quiet day. Use --seed,")
        print("   or check that kind-39999 taggings actually reach this server.")
    elif data.get("empty_reason") == "no_qualifying_asserters":
        print("\n!! Taggings exist but no asserter clears the rank threshold under")
        print("   this observer — is the observer scored (influence_* in Neo4j)?")
    elif data.get("published", 0) > 0:
        print(f"\nOK: published {data['published']} list(s), "
              f"retracted {data['retracted']}, failed {data['failed']}")

    if weighted and not any(t["slug"] == weighted["slug"] for t in data.get("tags", [])):
        print(f"\n!! Seeded tag '{weighted['slug']}' missing from the run — "
              "ingest may not have caught up; re-run without --seed-weighted.")

    if seeded and not any(t["slug"] == seeded["slug"] for t in data.get("tags", [])):
        print(f"\n!! Seeded tag '{seeded['slug']}' missing from the run — "
              "ingest may not have caught up; re-run without --seed.")

    if args.verify_relay and data.get("signing_pubkey"):
        events = asyncio.run(read_relay(data["signing_pubkey"]))
        print(f"\nOn the relay, signed by {data['signing_pubkey'][:8]}…: "
              f"{len(events)} kind-30392 event(s)")
        for ev in events:
            tags = {t[0]: t[1:] for t in ev["tags"]}
            p_tags = [t for t in ev["tags"] if t[0] == "p"]
            retracted = ["status", "retracted"] in ev["tags"]
            rigor = tags.get("rigor", ["-"])[0]
            print(f"  d={tags.get('d', ['?'])[0]}  title={tags.get('title', ['?'])[0]!r}  "
                  f"members={len(p_tags)}  rigor={rigor}"
                  f"{'  RETRACTED' if retracted else ''}")
            # D12: the score is the 3rd element of the p tag, after an empty
            # relay slot. Printed in wire order, which IS score-descending.
            for t in p_tags:
                score = t[3] if len(t) > 3 else "(none)"
                label = ""
                if weighted:
                    label = weighted["expected"].get(t[1], ("", None))[0]
                print(f"      score={score:>6}  {t[1][:16]}…  {label}")


if __name__ == "__main__":
    main()
