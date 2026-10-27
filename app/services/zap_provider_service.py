"""The key of the Observer's lightning service provider (NIP-57).

An Observer's kind-0 names their lightning address (`lud16`, or a bech32
`lud06` LNURL). Its LNURL-pay endpoint answers with `nostrPubkey`, the key that
signs the Observer's zap receipts (kind 9735) when `allowsNostr` is true. The
Observer chose that provider, so GrapeRank pins its key at the same fixed 95 as
the keys in their kind-10040: the calc request carries it in
`designated_pubkeys` alongside them.

Read fresh on every enqueue, no cache — like the 10040, it is one small read
per run, done in parallel with the 10040 lookup, and a changed provider is
picked up on the next run.

Best-effort: any miss or error yields None and the run proceeds without it.

The LNURL host comes from a profile anyone can write, so the GET is fenced:
https only, no redirects, a capped body, and a host that must resolve only to
public addresses. httpx resolves the name again when it connects, so a DNS
rebind between the two lookups is not covered; the request is a GET to a
fixed `/.well-known/lnurlp/` path (or the LNURL's own) whose answer is only
ever read for one pubkey.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit

import httpx

from app.core.loggr import loggr
from app.core.search_relay import fetch_profiles

logger = loggr.get_logger(__name__)

_HEX_PUBKEY = re.compile(r"^[0-9a-f]{64}$")
# LUD-16: the username is a-z0-9-_.+ ; the domain a plain hostname.
_LUD16 = re.compile(r"^([a-z0-9\-_.+]+)@([a-z0-9\-]+(?:\.[a-z0-9\-]+)+)$")

# The kind-0 from our search relay (neofry does not sync profiles), then one
# third-party HTTP GET. Bounded as a whole so a slow host can't hold up the
# enqueue for longer than the 10040 read it runs beside.
_STEP_TIMEOUT_SECONDS = 5.0
_TOTAL_TIMEOUT_SECONDS = 8.0
_MAX_BODY_BYTES = 64 * 1024

# One shared keep-alive client, lazily built — same pattern as app/core/flash.py.
# Building one loads the CA bundle synchronously (~100 ms on the event loop).
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(_STEP_TIMEOUT_SECONDS), follow_redirects=False
        )
    return _client


async def aclose() -> None:
    """Close the shared client. Called from the FastAPI lifespan shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def decode_lnurl(lnurl: str) -> str | None:
    """The URL a bech32 `lnurl1…` encodes, or None. LNURLs are longer than
    BIP-173's 90 characters, so this is bech32 without the length limit."""
    s = lnurl.strip().lower().removeprefix("lightning:")
    hrp, sep, data = s.rpartition("1")
    if hrp != "lnurl" or not sep or len(data) < 6:
        return None
    try:
        values = [_BECH32_CHARSET.index(c) for c in data]
    except ValueError:
        return None
    hrp_expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    if _bech32_polymod(hrp_expanded + values) != 1:
        return None
    acc, bits, out = 0, 0, bytearray()
    for v in values[:-6]:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def lnurl_pay_url(metadata: dict) -> str | None:
    """The LNURL-pay endpoint a kind-0's content points at: `lud16` first, as
    NIP-57 reads it, else `lud06`. None when neither is usable."""
    lud16 = metadata.get("lud16")
    if isinstance(lud16, str):
        m = _LUD16.match(lud16.strip().lower())
        if m:
            return f"https://{m.group(2)}/.well-known/lnurlp/{m.group(1)}"
    lud06 = metadata.get("lud06")
    if isinstance(lud06, str):
        url = decode_lnurl(lud06)
        if url and urlsplit(url).scheme == "https":
            return url
    return None


def parse_zap_provider_pubkey(body: object, observer: str) -> str | None:
    """`nostrPubkey` from an LNURL-pay answer that `allowsNostr`, or None.
    Never the Observer's own key."""
    if not isinstance(body, dict) or body.get("allowsNostr") is not True:
        return None
    pubkey = body.get("nostrPubkey")
    if not isinstance(pubkey, str):
        return None
    pubkey = pubkey.lower()
    if not _HEX_PUBKEY.match(pubkey) or pubkey == observer:
        return None
    return pubkey


async def _resolves_publicly(host: str, port: int) -> bool:
    """True when every address `host` resolves to is a public one."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(
        ipaddress.ip_address(a.split("%", 1)[0]).is_global for a in addresses
    )


async def _fetch_lnurl_pay(url: str) -> object | None:
    """The JSON an LNURL-pay endpoint answers with, or None when the URL isn't
    one we will fetch or the answer isn't JSON within the size cap."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username:
        return None
    if not await _resolves_publicly(parts.hostname, parts.port or 443):
        return None
    async with _get_client().stream(
        "GET", url, headers={"Accept": "application/json"}
    ) as response:
        if response.status_code != 200:
            return None
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > _MAX_BODY_BYTES:
                return None
    try:
        return json.loads(body)
    except ValueError:
        return None


async def _lookup(observer: str) -> str | None:
    # Our own relay verifies what it stores, so the kind-0 needs no signature check.
    event = (await fetch_profiles([observer])).get(observer)
    if event is None:
        return None
    try:
        metadata = json.loads(event.get("content") or "")
    except ValueError:
        return None
    if not isinstance(metadata, dict):
        return None
    url = lnurl_pay_url(metadata)
    if url is None:
        return None
    return parse_zap_provider_pubkey(await _fetch_lnurl_pay(url), observer)


async def fetch_zap_provider_pubkey(observer: str) -> str | None:
    """The `nostrPubkey` of `observer`'s lightning provider, or None when they
    have none, it doesn't do zaps, or it can't be read."""
    try:
        async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
            return await _lookup(observer)
    except Exception as e:
        logger.warning(f"zap provider lookup failed for {observer}: {e!r}")
        return None
