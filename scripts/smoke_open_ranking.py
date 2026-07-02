#!/usr/bin/env python3
"""Smoke-test the Open Ranking endpoints against a live deployment.

Mints a fresh NWT (signed kind-27519 event) on every request and exercises
every endpoint.

Usage:
    # Default: hit staging on arrowhead, generate a one-off signing key.
    python scripts/smoke_open_ranking.py

    # Target a different host:
    python scripts/smoke_open_ranking.py --base-url https://api.example.com

    # Use a specific nsec (bech32 or hex):
    python scripts/smoke_open_ranking.py --nsec nsec1...

    # Probe just one endpoint:
    python scripts/smoke_open_ranking.py --only stats

Requires: requests, nostr-sdk (both already in the server's poetry env).
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
    """Sign a kind-27519 event with aud + exp claims, return the
    base64url-no-padding-encoded JSON ready to drop into an Authorization
    header as `Nostr <token>`.
    """
    exp = int(time.time()) + ttl_seconds
    event = (
        EventBuilder(Kind(NWT_KIND), "open-ranking smoke test")
        .tags([Tag.parse(["aud", audience]), Tag.parse(["exp", str(exp)])])
        .sign_with_keys(keys)
    )
    return (
        base64.urlsafe_b64encode(event.as_json().encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )


def call(
    base_url: str,
    path: str,
    *,
    method: str = "POST",
    body: dict | None = None,
    keys: Keys | None = None,
    audience: str | None = None,
    timeout: float = 15.0,
) -> requests.Response:
    headers = {"content-type": "application/json"}
    if keys is not None and audience is not None:
        headers["Authorization"] = f"Nostr {mint_nwt(keys, audience)}"
    url = base_url.rstrip("/") + path
    return requests.request(
        method,
        url,
        headers=headers,
        data=json.dumps(body) if body is not None else None,
        timeout=timeout,
    )


def show(name: str, resp: requests.Response, verbose: bool) -> bool:
    ok = 200 <= resp.status_code < 300
    icon = "OK  " if ok else "FAIL"
    reason = resp.headers.get("x-reason", "")
    print(f"[{icon}] {name:<28} {resp.status_code} {reason}".rstrip())
    if not ok or verbose:
        body = resp.text
        if len(body) > 600:
            body = body[:600] + "..."
        print(f"       body: {body}")
    return ok


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--base-url",
        default="https://brainstormserver-staging.nosfabrica.com",
    )
    p.add_argument(
        "--audience",
        default=None,
        help="aud claim to sign. Default: hostname of --base-url.",
    )
    p.add_argument(
        "--nsec",
        default=None,
        help="Signing key (bech32 nsec or hex). Default: generate fresh.",
    )
    p.add_argument(
        "--target-pubkey",
        default="be7bf5de068c1d842ed34a7c270507ec940f5ea51671cfd062a95e9d09420d0a",
        help="Pubkey to query about. Default: the periodic-graperank pubkey.",
    )
    p.add_argument(
        "--only",
        choices=["wellknown", "stats", "rank", "search", "followers", "muters", "noauth"],
        default=None,
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    audience = args.audience or urlparse(args.base_url).hostname
    keys = Keys.parse(args.nsec) if args.nsec else Keys.generate()
    target = args.target_pubkey

    print(f"base_url : {args.base_url}")
    print(f"audience : {audience}")
    print(f"signer   : {keys.public_key().to_hex()}")
    print(f"target   : {target}")
    print("-" * 72)

    pass_count = 0
    fail_count = 0

    def record(ok: bool) -> None:
        nonlocal pass_count, fail_count
        pass_count += int(ok)
        fail_count += int(not ok)

    suite = args.only or "all"

    if suite in ("all", "wellknown"):
        r = call(args.base_url, "/.well-known/open-ranking.json", method="GET")
        record(show("GET  /.well-known", r, args.verbose))

    # Negative control: same endpoint without auth must be 401.
    if suite in ("all", "noauth"):
        r = call(args.base_url, "/stats/pubkey", body={"pubkey": target})
        ok = r.status_code == 401
        print(
            f"[{'OK  ' if ok else 'FAIL'}] no-auth control            "
            f"{r.status_code} (expected 401) "
            f"{r.headers.get('x-reason', '')}".rstrip()
        )
        record(ok)

    if suite in ("all", "stats"):
        r = call(
            args.base_url, "/stats/pubkey",
            body={"pubkey": target}, keys=keys, audience=audience,
        )
        record(show("POST /stats/pubkey", r, args.verbose))

    if suite in ("all", "rank"):
        r = call(
            args.base_url, "/rank/pubkeys",
            body={"pubkeys": [target]}, keys=keys, audience=audience,
        )
        record(show("POST /rank/pubkeys", r, args.verbose))

    if suite in ("all", "search"):
        r = call(
            args.base_url, "/search/pubkeys",
            body={"query": "jack"}, keys=keys, audience=audience,
        )
        record(show("POST /search/pubkeys", r, args.verbose))

    if suite in ("all", "followers"):
        r = call(
            args.base_url, "/followers",
            body={"pubkey": target, "limit": 5}, keys=keys, audience=audience,
        )
        record(show("POST /followers", r, args.verbose))

    if suite in ("all", "muters"):
        r = call(
            args.base_url, "/muters",
            body={"pubkey": target, "limit": 5}, keys=keys, audience=audience,
        )
        record(show("POST /muters", r, args.verbose))

    print("-" * 72)
    print(f"summary  : {pass_count} passed, {fail_count} failed")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
