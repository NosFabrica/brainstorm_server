import re

from fastapi import APIRouter, Query
from nostr_sdk import PublicKey

from app.core.meilisearch import (
    NOSTR_PROFILES_INDEX,
    get_document,
    search_index,
)
from app.schemas.request_response_schemas import (
    SearchByTextResponse,
    SearchResults,
)

router = APIRouter()

RANK_FIELD = "rank_be7bf5de068c1d842ed34a7c270507ec940f5ea51671cfd062a95e9d09420d0a"
SEARCH_ATTRIBUTES = ["name", "display_name", "about"]
SEARCH_LIMIT = 1000
RESULTS_LIMIT = 100

HEX_PUBKEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# Strip control characters; keep printable text only.
SANITIZE_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize(text: str) -> str:
    return SANITIZE_RE.sub("", text).strip()


def _try_resolve_pubkey(text: str) -> str | None:
    if HEX_PUBKEY_RE.match(text):
        return text.lower()
    if text.startswith("npub1"):
        try:
            return PublicKey.parse(text).to_hex()
        except Exception:
            return None
    return None


@router.get(
    path="/byText",
    summary="Search Nostr profiles by free-text, npub, or hex pubkey",
)
async def search_by_text_endpoint(
    text: str = Query(..., min_length=1, max_length=50),
) -> SearchByTextResponse:
    sanitized = _sanitize(text)

    pubkey = _try_resolve_pubkey(sanitized)
    if pubkey is not None:
        doc = await get_document(NOSTR_PROFILES_INDEX, pubkey)
        hits = [doc] if doc is not None else []
    else:
        hits = await search_index(
            NOSTR_PROFILES_INDEX,
            query=sanitized,
            attributes_to_search_on=SEARCH_ATTRIBUTES,
            limit=SEARCH_LIMIT,
        )

    hits.sort(key=lambda h: h.get(RANK_FIELD) or 0, reverse=True)
    results = hits[:RESULTS_LIMIT]

    return SearchByTextResponse(
        data=SearchResults(
            query=sanitized,
            numResults=len(results),
            results=results,
        )
    )
