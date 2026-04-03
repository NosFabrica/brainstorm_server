"""
Test script for GET /admin/brainstormPubkey/{nostr_pubkey}

Usage:
  python test_admin_brainstorm_pubkey.py
  python test_admin_brainstorm_pubkey.py --target-pubkey abc123...
  python test_admin_brainstorm_pubkey.py --base-url http://localhost:8000
"""

import argparse
import getpass
import json

import httpx
from nostr_sdk import Keys, PublicKey, EventBuilder, Kind, Tag


def main():
    parser = argparse.ArgumentParser(description="Test /admin/brainstormPubkey")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--target-pubkey", help="Pubkey to calculate for. Defaults to your own.")
    args = parser.parse_args()

    nsec = getpass.getpass("Enter nsec: ")
    keys = Keys.parse(nsec)
    pubkey = keys.public_key().to_hex()
    if args.target_pubkey:
        target = PublicKey.parse(args.target_pubkey).to_hex()
    else:
        target = pubkey

    print(f"Pubkey: {pubkey}")
    print(f"Target: {target}\n")

    # 1. Auth challenge
    r = httpx.get(f"{args.base_url}/authChallenge/{pubkey}")
    r.raise_for_status()
    challenge = r.json()["data"]["challenge"]

    # 2. Sign and verify
    event = (
        EventBuilder(Kind(27235), "")
        .tags([
            Tag.parse(["t", "brainstorm_login"]),
            Tag.parse(["challenge", challenge]),
        ])
        .sign_with_keys(keys)
    )
    r = httpx.post(
        f"{args.base_url}/authChallenge/{pubkey}/verify",
        json={"signed_event": json.loads(event.as_json())},
    )
    r.raise_for_status()
    token = r.json()["data"]["token"]

    # 3. Call admin route
    r = httpx.get(
        f"{args.base_url}/admin/brainstormPubkey/{target}",
        headers={"Authorization": f"Bearer {token}"},
    )
    print(f"Status: {r.status_code}")
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text)


if __name__ == "__main__":
    main()
