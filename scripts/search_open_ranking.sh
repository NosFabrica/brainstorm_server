#!/usr/bin/env bash
# Test the Open Ranking search endpoint (ORE-05: POST /search/pubkeys) with NO
# auth. ORE-05 auth is optional (optional_nwt_signer) and currently turned off,
# so a plain unsigned POST works — handy for quick testing.
#
# To rank from a specific observer, pass --pov <hex>. NOTE: pov only takes
# effect with the *personalized* algorithm (relevance-pov); the default
# algorithm (relevance) is global and ignores pov per ORE-01. This script
# auto-selects relevance-pov whenever you pass --pov (override with --algo).
# (To rank AS a signed observer you need the Python version,
# search_open_ranking.py, which signs the NWT.)
#
# Usage:   ./search_open_ranking.sh "<query>" [limit] [--pov <hex>] [--algo <id>] [--tsv]
# Env:     BRAINSTORM_HTTP  (default: staging server)
#
# Requires: curl, python3 (stdlib only).
set -euo pipefail

HTTP_BASE="${BRAINSTORM_HTTP:-https://brainstormserver-staging.nosfabrica.com}"

TSV=0
POV=""
ALGO=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --tsv) TSV=1 ;;
    --pov) shift; POV="${1:?--pov needs a hex pubkey}" ;;
    --algo) shift; ALGO="${1:?--algo needs an algorithm id}" ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
QUERY="${ARGS[0]:?usage: search_open_ranking.sh \"<query>\" [limit] [--pov <hex>] [--algo <id>] [--tsv]}"
LIMIT="${ARGS[1]:-15}"

# pov is honored only by a pov-based algorithm; default to the personalized one.
[ -n "$POV" ] && [ -z "$ALGO" ] && ALGO="relevance-pov"

BODY=$(python3 -c 'import json,sys; b={"query":sys.argv[1],"limit":int(sys.argv[2])}; \
pov,algo=sys.argv[3],sys.argv[4]; \
b.update({"pov":pov} if pov else {}); b.update({"algorithm":algo} if algo else {}); \
print(json.dumps(b))' "$QUERY" "$LIMIT" "$POV" "$ALGO")

[ "$TSV" -eq 0 ] && echo "ORE-05  $HTTP_BASE/search/pubkeys  query=$(printf '%q' "$QUERY")  pov=${POV:-<default>}  algo=${ALGO:-<default>}" >&2

# POST the search; capture body + HTTP status (last line).
resp=$(curl -s -w $'\n%{http_code}' -X POST "$HTTP_BASE/search/pubkeys" \
  -H 'content-type: application/json' -d "$BODY")
code="${resp##*$'\n'}"
results_json="${resp%$'\n'*}"

# Stash the (potentially large) JSON blobs in temp files — passing full profile
# payloads through env vars overflows ARG_MAX ("Argument list too long").
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
printf '%s' "$results_json" > "$tmp/results.json"

CODE="$code" RESULTS_FILE="$tmp/results.json" TSV="$TSV" python3 <<'PY'
import os, json, sys

tsv = os.environ.get("TSV") == "1"
code = os.environ.get("CODE", "")
results_text = open(os.environ["RESULTS_FILE"]).read()
if not code.startswith("2"):
    print(f"[FAIL] HTTP {code}", file=sys.stderr)
    print(results_text[:800], file=sys.stderr)
    sys.exit(1)

results = json.loads(results_text).get("results", [])
for i, r in enumerate(results):
    pk = r.get("pubkey", "")
    rank = r.get("rank")
    if tsv:
        print(f"{i}\t{pk}\t{rank}")
    else:
        print(f"{i:2}  rank={rank}  {pk}")
if not results and not tsv:
    print("(no results)", file=sys.stderr)
PY
