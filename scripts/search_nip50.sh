#!/usr/bin/env bash
# Test the NIP-50 search relay (wss .../relay) from the command line.
#
# Sends a NIP-01 REQ with a `search` filter, waits for EVENT frames, and prints
# them in the order the relay returned them (i.e. Vespa rank order). The relay
# only hands back kind-0 profile events, so we show display_name/name + pubkey.
#
# NIP-50 extension tokens go INSIDE the search string, so you can reproduce
# per-observer / sort / filter behavior directly, e.g.:
#   ./search_nip50.sh "cloud"
#   ./search_nip50.sh "cloud observer:<64-hex-pubkey>"   # rank from that POV
#   ./search_nip50.sh "cloud sort:rank:desc"             # pure trust order
#   ./search_nip50.sh "cloud filter:rank:gte:50"         # drop low-trust hits
#
# Usage:   ./search_nip50.sh "<search string>" [limit] [--tsv]
# Env:     BRAINSTORM_WS  (default: staging relay)
#          NIP50_WAIT     seconds to keep the socket open (default 6)
#
# Requires: websocat, python3 (stdlib only).
set -euo pipefail

WS_URL="${BRAINSTORM_WS:-wss://brainstormserver-staging.nosfabrica.com/relay}"
NIP50_WAIT="${NIP50_WAIT:-6}"

TSV=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --tsv) TSV=1 ;;
    *) ARGS+=("$a") ;;
  esac
done
QUERY="${ARGS[0]:?usage: search_nip50.sh \"<search string>\" [limit] [--tsv]}"
LIMIT="${ARGS[1]:-15}"

REQ=$(python3 -c 'import json,sys; print(json.dumps(["REQ","s1",{"kinds":[0],"search":sys.argv[1],"limit":int(sys.argv[2])}]))' "$QUERY" "$LIMIT")

[ "$TSV" -eq 0 ] && echo "NIP-50  $WS_URL  search=$(printf '%q' "$QUERY")" >&2

( printf '%s\n' "$REQ"; sleep "$NIP50_WAIT" ) \
  | timeout "$((NIP50_WAIT + 6))" websocat -t "$WS_URL" 2>/dev/null \
  | TSV="$TSV" python3 -c '
import sys, os, json
tsv = os.environ.get("TSV") == "1"
i = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        m = json.loads(line)
    except ValueError:
        continue
    if not isinstance(m, list) or not m:
        continue
    if m[0] == "EVENT" and len(m) >= 3:
        ev = m[2]
        pk = ev.get("pubkey", "")
        try:
            c = json.loads(ev.get("content", "{}"))
        except ValueError:
            c = {}
        nm = c.get("display_name") or c.get("name") or "?"
        if tsv:
            print(f"{i}\t{pk}\t{nm}")
        else:
            print(f"{i:2}  {nm[:28]:28}  {pk[:16]}")
        i += 1
    elif m[0] == "NOTICE" and not tsv:
        print(f"NOTICE: {m[1] if len(m) > 1 else m}", file=sys.stderr)
if i == 0 and not tsv:
    print("(no results — check the query, the relay URL, or NIP50_WAIT)", file=sys.stderr)
'
