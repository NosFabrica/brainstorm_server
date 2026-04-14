"""
Fetch a short-lived admin JWT for the brainstorm-server, printed to stdout.

Use with Postman, curl, or any HTTP client that can't sign Nostr events.
Token lifetime is controlled by `auth_access_token_expire_minutes` on the
server (default 60). Your pubkey must be in `admin_whitelisted_pubkeys`.

Usage:
  python3 scripts/get_admin_token.py
  python3 scripts/get_admin_token.py --base-url http://localhost:8000
  TOKEN=$(python3 scripts/get_admin_token.py)
  curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/admin/...
"""

import argparse
import getpass
import json
import sys

import httpx
from nostr_sdk import EventBuilder, Keys, Kind, Tag


def fetch_admin_token(base_url: str, keys: Keys) -> str:
    pubkey = keys.public_key().to_hex()

    r = httpx.get(f"{base_url}/authChallenge/{pubkey}")
    r.raise_for_status()
    challenge = r.json()["data"]["challenge"]

    event = (
        EventBuilder(Kind(22242), "")
        .tags([
            Tag.parse(["t", "brainstorm_login"]),
            Tag.parse(["challenge", challenge]),
        ])
        .sign_with_keys(keys)
    )
    r = httpx.post(
        f"{base_url}/authChallenge/{pubkey}/verify",
        json={"signed_event": json.loads(event.as_json())},
    )
    r.raise_for_status()
    return r.json()["data"]["token"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    nsec = getpass.getpass("Enter nsec: ")
    keys = Keys.parse(nsec)
    print(f"Pubkey: {keys.public_key().to_hex()}", file=sys.stderr)

    token = fetch_admin_token(args.base_url, keys)
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
