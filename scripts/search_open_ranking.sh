#!/usr/bin/env bash
# Test the Open Ranking search endpoint (ORE-05: POST /search/pubkeys) with NO
# auth. ORE-05 auth is optional (optional_nwt_signer) and currently turned off,
# so a plain unsigned POST works — handy for quick testing.
#
# To rank from a specific observer, pass --pov <hex>. NOTE: pov only takes
# effect with the *personalized* algorithm (name-trust-pov); the default
# algorithm (name-trust) is global and ignores pov per ORE-01. This script
# auto-selects name-trust-pov whenever you pass --pov (override with --algo).
# (To rank AS a signed observer you need the Python version,
# search_open_ranking.py, which signs the NWT.)
#
# The ORE-05 response is just {pubkey, rank}; this best-effort annotates each
# pubkey with its profile name via GET /search/byText so the output is readable.
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
[ -n "$POV" ] && [ -z "$ALGO" ] && ALGO="name-trust-pov"

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

# Best-effort name lookup (public, no auth).
curl -s -G "$HTTP_BASE/search/byText" \
  --data-urlencode "text=$QUERY" --data-urlencode "maxHits=400" \
  --data-urlencode "onlyRanked=false" > "$tmp/names.json" 2>/dev/null || echo '{}' > "$tmp/names.json"

CODE="$code" RESULTS_FILE="$tmp/results.json" NAMES_FILE="$tmp/names.json" TSV="$TSV" python3 <<'PY'
import os, json, sys

tsv = os.environ.get("TSV") == "1"
code = os.environ.get("CODE", "")
results_text = open(os.environ["RESULTS_FILE"]).read()
if not code.startswith("2"):
    print(f"[FAIL] HTTP {code}", file=sys.stderr)
    print(results_text[:800], file=sys.stderr)
    sys.exit(1)

def find_list(o):
    if isinstance(o, list) and o and isinstance(o[0], dict):
        return o
    if isinstance(o, dict):
        for v in o.values():
            got = find_list(v)
            if got:
                return got
    return None

# Build pubkey -> name from the byText response.
names = {}
try:
    with open(os.environ["NAMES_FILE"]) as fh:
        for r in find_list(json.load(fh)) or []:
            pk = r.get("pubkey")
            if isinstance(pk, str):
                names[pk] = r.get("display_name") or r.get("name") or "?"
except Exception:
    pass

results = json.loads(results_text).get("results", [])
for i, r in enumerate(results):
    pk = r.get("pubkey", "")
    rank = r.get("rank")
    nm = names.get(pk, "?")
    if tsv:
        print(f"{i}\t{pk}\t{nm}\t{rank}")
    else:
        print(f"{i:2}  {str(nm)[:28]:28}  rank={rank}  {pk[:16]}")
if not results and not tsv:
    print("(no results)", file=sys.stderr)
PY
