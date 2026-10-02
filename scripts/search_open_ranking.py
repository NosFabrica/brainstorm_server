#!/usr/bin/env python3
"""Test the Open Ranking search endpoint (ORE-05: POST /search/pubkeys).

Returns pubkeys ranked by the observer's quality score. The observer is chosen
(in priority order) by:

  * a signed request  -> the signer's pubkey IS the observer (`--nsec` / `--sign`)
  * `--pov <hex>`     -> that pubkey (sent unsigned; auth on this endpoint is optional)
  * neither           -> the server's default observer

That makes it easy to reproduce per-observer ranking differences (e.g. why a
NIP-50 client showed a different order): just vary `--pov`.

Usage:
    # default observer, against staging:
    python scripts/search_open_ranking.py --query cloud

    # rank from a specific point of view (no signing needed):
    python scripts/search_open_ranking.py --query cloud --pov <64-hex-pubkey>

    # sign the request so the observer is the signer (fresh key by default):
    python scripts/search_open_ranking.py --query cloud --sign
    python scripts/search_open_ranking.py --query cloud --nsec nsec1...

Requires: requests, nostr-sdk (both in the server's poetry env).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from urllib.parse import urlparse

import requests
from nostr_sdk import EventBuilder, Keys, Kind, Tag

NWT_KIND = 27519


def mint_nwt(keys: Keys, audience: str, ttl_seconds: int = 300) -> str:
    """Sign a kind-27519 NWT and base64url-encode it for `Authorization: Nostr`."""
    exp = int(time.time()) + ttl_seconds
    event = (
        EventBuilder(Kind(NWT_KIND), "open-ranking search test")
        .tags([Tag.parse(["aud", audience]), Tag.parse(["exp", str(exp)])])
        .sign_with_keys(keys)
    )
    return (
        base64.urlsafe_b64encode(event.as_json().encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--base-url", default="https://brainstormserver-staging.nosfabrica.com"
    )
    p.add_argument("--query", required=True, help="free-text search query")
    p.add_argument("--pov", default=None, help="observer pubkey (hex) to rank from")
    p.add_argument(
        "--algo",
        default=None,
        help=(
            "ORE-05 algorithm id. pov only applies to the personalized algorithm "
            "(relevance-pov), so --pov auto-selects it unless --algo is given."
        ),
    )
    p.add_argument("--limit", type=int, default=15)
    p.add_argument(
        "--nsec", default=None, help="sign with this key (bech32/hex); implies --sign"
    )
    p.add_argument(
        "--sign",
        action="store_true",
        help="sign the request so the observer = signer (generates a key if no --nsec)",
    )
    p.add_argument("--audience", default=None, help="aud claim; default = host of base-url")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    body: dict = {"query": args.query, "limit": args.limit}
    headers = {"content-type": "application/json"}
    observer_desc = "server default"

    # pov is honored only by a pov-based algorithm; default to the personalized
    # one when --pov is given (the default relevance algorithm ignores pov).
    algo = args.algo or ("relevance-pov" if args.pov else None)
    if algo:
        body["algorithm"] = algo

    if args.nsec or args.sign:
        keys = Keys.parse(args.nsec) if args.nsec else Keys.generate()
        audience = args.audience or urlparse(args.base_url).hostname
        headers["Authorization"] = f"Nostr {mint_nwt(keys, audience)}"
        observer_desc = f"signer {keys.public_key().to_hex()[:16]}…"
    elif args.pov:
        body["pov"] = args.pov
        observer_desc = f"pov {args.pov[:16]}…"

    url = args.base_url.rstrip("/") + "/search/pubkeys"
    print(f"POST {url}")
    print(f"query    : {args.query!r}")
    print(f"observer : {observer_desc}")
    print("-" * 72)

    resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=20)
    if not (200 <= resp.status_code < 300):
        print(f"[FAIL] {resp.status_code} {resp.headers.get('x-reason', '')}")
        print(resp.text[:800])
        return 1

    results = resp.json().get("results", [])
    for i, r in enumerate(results):
        print(f"{i:2}  rank={r.get('rank')}  {r.get('pubkey', '')}")
    if not results:
        print("(no results)")
    if args.verbose:
        print("-" * 72)
        print(json.dumps(resp.json(), indent=2)[:1200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
