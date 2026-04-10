"""
Test script for /admin/nsec-encryption/{rotate,verify}

Usage:
  python test_admin_nsec_encryption.py verify
  python test_admin_nsec_encryption.py rotate
  python test_admin_nsec_encryption.py rotate --base-url http://localhost:8000
"""

import argparse
import getpass
import json
import sys
from pathlib import Path

import httpx
from nostr_sdk import Keys

sys.path.insert(0, str(Path(__file__).parent))
from get_admin_token import fetch_admin_token  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Test /admin/nsec-encryption")
    parser.add_argument("action", choices=["rotate", "verify"])
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    nsec = getpass.getpass("Enter nsec: ")
    keys = Keys.parse(nsec)
    print(f"Pubkey: {keys.public_key().to_hex()}\n")

    token = fetch_admin_token(args.base_url, keys)

    r = httpx.post(
        f"{args.base_url}/admin/nsec-encryption/{args.action}",
        headers={"Authorization": f"Bearer {token}"},
    )
    print(f"Status: {r.status_code}")
    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text)


if __name__ == "__main__":
    main()
