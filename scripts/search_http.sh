#!/usr/bin/env bash
# Test the HTTP profile search (GET /search/byText) from the command line.
#
# Prints results in Vespa rank order with the relevance score and the observer's
# quality score (_quality_score) so you can see WHY they're ordered that way.
# This is the anonymous / default-observer path (the same one the UI uses when
# logged out). onlyRanked defaults to true (drops zero-trust hits); pass
# --all to include them.
#
# Usage:   ./search_http.sh "<query>" [maxHits] [--all] [--tsv]
# Env:     BRAINSTORM_HTTP  (default: staging server)
#
# Requires: curl, python3 (stdlib only).
set -euo pipefail

HTTP_BASE="${BRAINSTORM_HTTP:-https://brainstormserver-staging.nosfabrica.com}"

TSV=0
ONLY_RANKED=true
ARGS=()
for a in "$@"; do
  case "$a" in
    --tsv) TSV=1 ;;
    --all) ONLY_RANKED=false ;;
    *) ARGS+=("$a") ;;
  esac
done
QUERY="${ARGS[0]:?usage: search_http.sh \"<query>\" [maxHits] [--all] [--tsv]}"
MAXHITS="${ARGS[1]:-15}"

[ "$TSV" -eq 0 ] && echo "HTTP    $HTTP_BASE  text=$(printf '%q' "$QUERY")  onlyRanked=$ONLY_RANKED" >&2

curl -s -G "$HTTP_BASE/search/byText" \
  --data-urlencode "text=$QUERY" \
  --data-urlencode "maxHits=$MAXHITS" \
  --data-urlencode "onlyRanked=$ONLY_RANKED" \
  | TSV="$TSV" python3 -c '
import sys, os, json
tsv = os.environ.get("TSV") == "1"

def find_list(o):
    if isinstance(o, list) and o and isinstance(o[0], dict):
        return o
    if isinstance(o, dict):
        for v in o.values():
            r = find_list(v)
            if r:
                return r
    return None

d = json.load(sys.stdin)
res = find_list(d) or []
for i, r in enumerate(res):
    pk = r.get("pubkey", "")
    nm = r.get("display_name") or r.get("name") or "?"
    rel = r.get("_relevance")
    qs = r.get("_quality_score")
    if tsv:
        print(f"{i}\t{pk}\t{nm}\t{rel}\t{qs}")
    else:
        rel_s = f"{rel:.1f}" if isinstance(rel, (int, float)) else str(rel)
        print(f"{i:2}  {nm[:28]:28}  rel={rel_s:>9}  qs={qs}  {pk[:16]}")
if not res and not tsv:
    print("(no results)", file=sys.stderr)
'
