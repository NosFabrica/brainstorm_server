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
import sys
from pathlib import Path

import httpx
from nostr_sdk import Keys, PublicKey

sys.path.insert(0, str(Path(__file__).parent))
from get_admin_token import fetch_admin_token  # noqa: E402


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

    token = fetch_admin_token(args.base_url, keys)

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
