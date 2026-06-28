#!/usr/bin/env bash
# Run the SAME query through the HTTP API and the NIP-50 relay and print the two
# ranked lists side by side, flagging where they diverge.
#
# Both paths share app.core.vespa.search, so with the default observer and a
# plain query they should match exactly. Divergence almost always means the
# NIP-50 side resolved a different observer (an `observer:` token in the search
# string, or a cold-start observer with no scores) or a sort:/filter: token.
#
# Usage:   ./search_compare.sh "<query>" [limit]
# Env:     BRAINSTORM_HTTP, BRAINSTORM_WS  (default: staging)
#
# Note: pass extra NIP-50 tokens by quoting them into the query, e.g.
#       ./search_compare.sh "cloud sort:rank:desc"
#       The HTTP side treats unknown tokens as plain text (harmless).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUERY="${1:?usage: search_compare.sh \"<query>\" [limit]}"
LIMIT="${2:-15}"

http_tsv="$("$DIR/search_http.sh" "$QUERY" "$LIMIT" --tsv 2>/dev/null || true)"
nip_tsv="$("$DIR/search_nip50.sh" "$QUERY" "$LIMIT" --tsv 2>/dev/null || true)"

HTTP_TSV="$http_tsv" NIP_TSV="$nip_tsv" QUERY="$QUERY" python3 <<'PY'
import os

def parse(blob):
    out = []
    for line in blob.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            out.append((parts[1], parts[2]))  # (pubkey, name)
    return out

http = parse(os.environ.get("HTTP_TSV", ""))
nip = parse(os.environ.get("NIP_TSV", ""))
query = os.environ.get("QUERY", "")

print()
print(f"query: {query!r}    HTTP={len(http)} hits   NIP-50={len(nip)} hits")
print()
print(f"{chr(35):>2}  {'HTTP /search/byText':30}  {'NIP-50 relay':30}  match")
print("-" * 74)
n = max(len(http), len(nip))
agree = True
for i in range(n):
    h = http[i] if i < len(http) else None
    g = nip[i] if i < len(nip) else None
    hn = h[1][:28] if h else "-"
    gn = g[1][:28] if g else "-"
    same = bool(h) and bool(g) and h[0] == g[0]
    if not same:
        agree = False
    print(f"{i:2}  {hn:30}  {gn:30}  {'ok' if same else '<>'}")
print("-" * 74)
print("IDENTICAL order" if agree and http and nip else "ORDER DIFFERS (see <> rows above)")
PY
