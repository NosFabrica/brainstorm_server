"""Probe the Vespa search ranking directly, bypassing the FastAPI app.

This exercises exactly the scoring path you'd tune (the YQL builder + ranking
features in app/core/vespa.py) against a *running, already-populated* Vespa.
Search is a read-only GET, so it's safe to point at staging/production data.

Usage:
    VESPA_URL=http://localhost:8080 \
    OBSERVER=<observer_hex_pubkey> \
        poetry run python scripts/search_scoring_probe.py "vitor pamplona"

    # only ranked (quality_score > 0) results:
    ... poetry run python scripts/search_scoring_probe.py --only-ranked "nosfab"

The required app settings are stubbed with dummy values before import so you
don't need a full .env — only VESPA_URL (and OBSERVER) actually matter here.
"""
import argparse
import asyncio
import os

# Satisfy the (many) required Settings() fields so importing app.core.vespa
# works without a real .env. setdefault keeps any real env / .env values.
_STUB_ENV = {
    "DB_URL": "postgresql+asyncpg://x:x@localhost:5432",
    "DEPLOY_ENVIRONMENT": "LOCAL",
    "AUTH_ALGORITHM": "HS256",
    "AUTH_SECRET_KEY": "x",
    "AUTH_ACCESS_TOKEN_EXPIRE_MINUTES": "60",
    "SQL_ADMIN_USERNAME": "x",
    "SQL_ADMIN_PASSWORD": "x",
    "SQL_ADMIN_SECRET_KEY": "x",
    "NEO4J_DB_URL": "neo4j://localhost:7687",
    "NEO4J_DB_USERNAME": "neo4j",
    "NEO4J_DB_PASSWORD": "x",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "NOSTR_TRANSFER_FROM_RELAY": "wss://x",
    "NOSTR_TRANSFER_TO_RELAY": "wss://x",
    "NOSTR_UPLOAD_TA_EVENTS_RELAY": "wss://x",
    "NOSTR_UPLOAD_TA_EVENTS_RELAY_PUBLIC_URL": "wss://x",
    "CUTOFF_OF_VALID_GRAPERANK_SCORES": "0.0",
    "PERFORM_NOSTR_FULL_SYNC": "false",
    "FRONTEND_URL": "http://localhost:3000",
    "PUBLIC_BASE_URL": "http://localhost:8000",
    "VESPA_URL": "http://localhost:8080",
}
for k, v in _STUB_ENV.items():
    os.environ.setdefault(k, v)

# Hardcoded default observer used by the search router when none is configured.
_DEFAULT_OBSERVER = (
    os.environ.get("OBSERVER")
    or os.environ.get("PERIODIC_GRAPERANK_PUBKEY")
    or "e8caa9e8aef0aa32f5f0e8b8c0e1c0e9c0e1c0e9c0e1c0e9c0e1c0e9c0e1c0e9"
)


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="free-text search query")
    parser.add_argument(
        "--observer", default=_DEFAULT_OBSERVER, help="observer hex pubkey"
    )
    parser.add_argument("--hits", type=int, default=20)
    parser.add_argument(
        "--only-ranked",
        action="store_true",
        help="drop results whose quality_score is 0",
    )
    parser.add_argument(
        "--show-yql", action="store_true", help="print the generated YQL"
    )
    args = parser.parse_args()

    from app.core import vespa  # imported after env is stubbed

    if args.show_yql:
        words = args.query.split()[: vespa.MAX_QUERY_WORDS]
        joined = "".join(words) if len(words) >= 2 else None
        print("YQL:", vespa._build_yql(words, joined), "\n")

    try:
        results = await vespa.search(
            query_text=args.query,
            user_pubkey=args.observer,
            hits=args.hits,
            include_zero_score_results=not args.only_ranked,
        )
    finally:
        await vespa.aclose()

    print(f'"{args.query}"  observer={args.observer[:12]}…  {len(results)} hits\n')
    print(f"{'rank':>4}  {'relevance':>10}  {'qscore':>6}  name / display_name")
    print("-" * 70)
    for i, r in enumerate(results, 1):
        name = r.get("name") or ""
        disp = r.get("display_name") or ""
        label = name if name == disp or not disp else f"{name}  ({disp})"
        rel = r.get("_relevance")
        q = r.get("_quality_score")
        rel_s = f"{rel:.4f}" if isinstance(rel, (int, float)) else str(rel)
        print(f"{i:>4}  {rel_s:>10}  {str(q):>6}  {label[:48]}")


if __name__ == "__main__":
    asyncio.run(_main())
