#!/usr/bin/env python3
"""Emit a signed Flash webhook at a running server.

There is no Flash sandbox, so this is how the webhook path gets exercised without
paying for a real subscription. It signs with the *real* secret and delivers over
real HTTP, so the server's verification path runs unmodified.

A script rather than a dev-only endpoint on purpose: there is no route to
accidentally expose in production, and no gating logic to get wrong.

Stdlib-only and app-free, like the other `search_*` helpers, so it runs against a
deployed host without a populated `.env`. The cost is that `sign()` below repeats
the server's HMAC; `tests/test_flash_webhook.py` asserts the two agree, so drift
fails the suite rather than silently producing deliveries nothing accepts.

    export FLASH_WEBHOOK_SECRET=whsec_...
    python -m scripts.emit_flash_webhook --event subscription.activated --ref <hex pubkey>

To exercise Flash's retry path, pin the event time so the body is byte-identical
across runs — otherwise each run is a genuinely new event and inserts a new row:

    python -m scripts.emit_flash_webhook --at 2026-08-24T12:00:00.000Z   # twice
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


def sign(secret: str, timestamp: int, raw_body: bytes) -> str:
    """HMAC-SHA256 over `{timestamp}.{raw body}` — mirrors the server's verifier."""
    return hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()

EVENT_EXTRAS = {
    "subscription.activated": lambda now: {"activatedAt": now},
    "subscription.renewed": lambda now: {
        "invoiceId": f"inv_{now}",
        "amount": 200,
        "currency": "USD",
        "paymentId": f"pay_{now}",
        "periodNumber": 2,
        "paidAt": now,
    },
    "subscription.past_due": lambda now: {"attemptNumber": 1, "firstFailedAt": now},
    "subscription.canceled": lambda now: {"canceledAt": now, "reason": "user_request"},
    "subscription.expired": lambda now: {"expiredAt": now},
}


def build_payload(args) -> dict:
    now = args.at or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    extras = EVENT_EXTRAS.get(args.event, lambda _: {})(now)
    return {
        "event": args.event,
        "timestamp": now,
        "data": {
            "accountId": args.account_id,
            "subscriptionId": args.subscription_id,
            "serviceId": args.service_id,
            "planId": args.plan_id,
            "subscriberId": args.subscriber_id,
            "externalRef": args.ref,
            **extras,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default="http://localhost:8000/webhooks/flash", help="Target endpoint"
    )
    parser.add_argument(
        "--event",
        default="subscription.activated",
        help="Event name. Anything is allowed — unrecognised names are recorded too.",
    )
    parser.add_argument("--ref", default="user_test", help="externalRef (our pubkey)")
    parser.add_argument("--subscription-id", default="sub_test")
    parser.add_argument("--subscriber-id", default="subscriber_test")
    parser.add_argument("--service-id", default="service_test")
    parser.add_argument("--plan-id", default="plan_test")
    parser.add_argument("--account-id", default="account_test")
    parser.add_argument(
        "--at",
        default=None,
        help="Pin the event time (ISO 8601). Repeat runs with the same value to "
        "exercise the retry path — the body, and so the dedupe key, is identical.",
    )
    parser.add_argument(
        "--skew",
        type=int,
        default=0,
        help="Seconds to offset the signature timestamp. Use a large value to "
        "exercise the replay-window rejection.",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("FLASH_WEBHOOK_SECRET", ""),
        help="Signing secret. Defaults to $FLASH_WEBHOOK_SECRET.",
    )
    parser.add_argument(
        "--tamper",
        action="store_true",
        help="Sign one body and send a different one, to exercise rejection.",
    )
    args = parser.parse_args()

    if not args.secret:
        print(
            "No signing secret. Set FLASH_WEBHOOK_SECRET or pass --secret.",
            file=sys.stderr,
        )
        return 2

    raw = json.dumps(build_payload(args)).encode()
    signed_body = raw
    if args.tamper:
        args.subscription_id = "tampered"
        raw = json.dumps(build_payload(args)).encode()

    timestamp = int(time.time()) + args.skew
    signature = sign(args.secret, timestamp, signed_body)

    request = urllib.request.Request(
        args.url,
        data=raw,
        headers={
            "Content-Type": "application/json",
            "Flash-Signature": f"t={timestamp},v1={signature}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request) as response:
            print(f"{response.status} {response.read().decode()}")
            return 0
    except urllib.error.HTTPError as err:
        print(f"{err.code} {err.read().decode()}", file=sys.stderr)
        return 1
    except urllib.error.URLError as err:
        print(f"Could not reach {args.url}: {err.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
