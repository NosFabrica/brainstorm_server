#!/usr/bin/env python3
"""Confirm both Flash credentials work, from outside the app.

Exists because "the rotation went fine" is otherwise an assumption. It exercises
the two things a rotation can break, independently, so a failure says which one:

  1. the API key, by asking Flash to read a subscription;
  2. the webhook secret, by signing a delivery and posting it at our own
     endpoint — the same path a real delivery takes, verified the same way.

    export FLASH_API_KEY=sk_live_...
    export FLASH_WEBHOOK_SECRET=whsec_...
    python -m scripts.check_flash_credentials --base-url https://<host>

Stdlib only and app-free, like the other check scripts, so it runs against a
deployed host without a populated .env. Exit code is 0 only if both pass.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

FLASH_SUBSCRIPTIONS_PATH = "/api/v1/external/subscriptions"
PROBE_EVENT = "credential.check"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """The server's construction, re-derived. tests/test_flash_rotation.py holds
    the two together — this file stays stdlib-only so it can run with no .env."""
    return hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()


def _get(url: str, headers: dict, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()
    except urllib.error.URLError as err:
        return 0, str(err.reason)


def check_api_key(flash_base: str, api_key: str, timeout: float) -> bool:
    """A lookup for a reference nobody has. 200 with no subscriptions is the
    pass: it proves the key was accepted, without needing a real subscriber."""
    url = f"{flash_base.rstrip('/')}{FLASH_SUBSCRIPTIONS_PATH}?ref=rotation-probe"
    status, body = _get(url, {"Authorization": f"Bearer {api_key}"}, timeout)

    if status == 200:
        print("API key      OK   (Flash accepted the key)")
        return True
    if status in (401, 403):
        print(f"API key      FAIL Flash refused it ({status}) — wrong or revoked key")
    elif status == 0:
        print(f"API key      FAIL could not reach Flash: {body}")
    else:
        print(f"API key      FAIL Flash answered {status}")
    return False


def check_webhook_secret(base_url: str, secret: str, timeout: float) -> bool:
    """Signs a delivery and posts it at our own endpoint.

    Uses an event name we do not act on, so a successful check cannot change
    anyone's tier — it is recorded and acknowledged, which is all we are asking.
    """
    body = json.dumps(
        {
            "event": PROBE_EVENT,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "data": {"subscriptionId": f"rotation-probe-{int(time.time())}"},
        }
    ).encode()
    timestamp = int(time.time())
    signature = sign(secret, timestamp, body)

    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/webhooks/flash",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Flash-Signature": f"t={timestamp},v1={signature}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            print(f"Webhook      OK   (accepted, {response.status})")
            return True
    except urllib.error.HTTPError as err:
        detail = err.read().decode()
        if err.code == 401:
            print(
                "Webhook      FAIL signature rejected — the server has a different secret"
            )
        elif err.code == 404:
            print(
                "Webhook      FAIL no endpoint — payments are not enabled on this host"
            )
        else:
            print(f"Webhook      FAIL {err.code} {detail}")
    except urllib.error.URLError as err:
        print(f"Webhook      FAIL could not reach the server: {err.reason}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default="http://localhost:8000", help="Our server"
    )
    parser.add_argument(
        "--flash-base-url", default="https://dev.server.vault.paywithflash.com"
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--skip-api", action="store_true", help="Check only the webhook secret"
    )
    parser.add_argument(
        "--skip-webhook", action="store_true", help="Check only the API key"
    )
    args = parser.parse_args()

    if args.skip_api and args.skip_webhook:
        print(
            "Nothing to check — skipping both leaves nothing verified.", file=sys.stderr
        )
        return 2

    # Environment only: argv is visible in `ps` and lands in shell history.
    api_key = os.environ.get("FLASH_API_KEY", "")
    secret = os.environ.get("FLASH_WEBHOOK_SECRET", "")

    results = []
    if not args.skip_api:
        if not api_key:
            print("API key      SKIP no FLASH_API_KEY given", file=sys.stderr)
            return 2
        results.append(check_api_key(args.flash_base_url, api_key, args.timeout))
    if not args.skip_webhook:
        if not secret:
            print("Webhook      SKIP no FLASH_WEBHOOK_SECRET given", file=sys.stderr)
            return 2
        results.append(check_webhook_secret(args.base_url, secret, args.timeout))

    ok = all(results)
    print(
        "\nRotation verified."
        if ok
        else "\nSomething is wrong — do not remove the old secret."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
