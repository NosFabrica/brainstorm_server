"""Explain how ONE pubkey matches + ranks for a query — a search debugger.

Given a pubkey and a query, this:
  1. fetches the doc's stored fields,
  2. runs the REAL search YQL (shared `app.core.vespa_query.build_query`, so it
     never drifts from production) restricted to that one doc, and dumps the
     decisive rank signals — matchCount per field, has_token_match,
     affiliation_match, match_quality, the derived tier, and relevance,
  3. runs the full search and reports where the doc actually lands.

Stdlib-only (urllib) and imports only the dependency-free query builder, so it
runs WITHOUT a populated .env — point it at a port-forwarded Vespa:

    kubectl -n <ns> port-forward svc/brainstorm-vespa 8080:8080 &
    VESPA_URL=http://localhost:8080 python -m scripts.analyze_pubkey <pubkey|npub> "<query>"

Flags:
    --profile P    rank profile (default sort_followers — the byText default)
    --observer HEX pass observer for user_q so _quality_score/_followers are real
    --vespa URL    overrides $VESPA_URL (default http://localhost:8080)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.vespa_query import build_query  # noqa: E402

SEARCH_FIELDS = ("name", "display_name", "username", "nip05", "lud16", "website", "about")


def _get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def _tier(mf: dict) -> str:
    mq = mf.get("match_quality") or 0
    if mq and int(mq) > 0:
        return {4: "exact", 3: "prefix", 2: "1-typo", 1: "2-typo"}.get(int(mq), str(mq))
    if mf.get("has_token_match"):
        return "name"
    if mf.get("affiliation_match"):
        return "affiliation"
    return "gram"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pubkey", help="hex pubkey (64 hex chars)")
    p.add_argument("query")
    p.add_argument("--profile", default="sort_followers")
    p.add_argument("--observer", default=None)
    p.add_argument("--vespa", default=None)
    args = p.parse_args()

    vespa = (args.vespa or os.environ.get("VESPA_URL", "http://localhost:8080")).rstrip("/")
    pk = args.pubkey.lower()

    # 1) stored fields
    doc = _get(f"{vespa}/document/v1/doc/doc/docid/{pk}").get("fields", {})
    if not doc:
        print(f"no doc for pubkey {pk}")
        return 1
    print(f"=== {pk[:16]}…  query={args.query!r}  profile={args.profile} ===")
    print("--- stored fields ---")
    for f in SEARCH_FIELDS:
        v = doc.get(f)
        if v:
            print(f"  {f:13} {v[:80]!r}")

    # 2) isolate this doc under the real YQL + rank features
    _words, yql, wparams = build_query(args.query)
    yql = yql.replace("where ", f'where pubkey contains "{pk}" and ', 1)
    params = {"yql": yql, "ranking": args.profile, "hits": "1", **wparams}
    if args.observer:
        params["ranking.features.query(user_q)"] = "{" + args.observer.lower() + ":1.0}"
    hit = (_get(f"{vespa}/search/?" + urllib.parse.urlencode(params))
           .get("root", {}).get("children", []) or [{}])[0]
    mf = hit.get("fields", {}).get("matchfeatures", {})

    # Which fields match, and via which clause — reliable per-field/kind probes
    # (a field-restricted "does this doc appear?"). NB Vespa's listFeatures
    # matchCount(...) proved unreliable here, so we probe instead.
    print("--- which fields match (per-clause) ---")

    def _probe(field: str, ann: str) -> bool:
        yq = f'select pubkey from doc where pubkey contains "{pk}" and ({{defaultIndex:"{field}"{ann}}}userInput(@q))'
        pp = {"yql": yq, "q": args.query, "ranking": "unranked", "hits": "1"}
        got = _get(f"{vespa}/search/?" + urllib.parse.urlencode(pp)).get("root", {}).get("children", [])
        return bool(got)

    kinds = [("exact", ""), ("prefix", ",prefix:true"),
             ("fuzzy", ",fuzzy:{maxEditDistance:1,prefixLength:2}")]
    any_match = False
    for f in SEARCH_FIELDS:
        got = [k for k, ann in kinds if _probe(f, ann)]
        if got:
            any_match = True
            print(f"  {f:13} matched via: {', '.join(got)}")
    if not any_match:
        print("  (no field matched — recall miss)")
    print(f"  has_token_match={mf.get('has_token_match')}  "
          f"affiliation_match={mf.get('affiliation_match')}  "
          f"match_quality={mf.get('match_quality')}")
    print(f"  TIER = {_tier(mf)}   relevance = {hit.get('relevance')}")

    # 3) actual rank position in the full result set
    fp = dict(params)
    fp["yql"] = build_query(args.query)[1]  # unfiltered
    fp["hits"] = "400"
    fp.pop("ranking.listFeatures", None)
    kids = _get(f"{vespa}/search/?" + urllib.parse.urlencode(fp)).get("root", {}).get("children", [])
    pos = next((i for i, h in enumerate(kids)
                if h.get("fields", {}).get("pubkey", "").lower() == pk), None)
    print(f"--- rank position: {pos if pos is not None else 'NOT in top 400'} "
          f"of {len(kids)} ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
