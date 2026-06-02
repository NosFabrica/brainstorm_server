import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from nostr_sdk import PublicKey

from app.core.config import settings
from app.core.vespa import get_document, search
from app.schemas.request_response_schemas import (
    SearchByTextResponse,
    SearchResults,
)
from app.utils.api_validators import verify_token_optional
from app.utils.auth.auth_models import JWTData

router = APIRouter()

# Hardcoded observer pubkey used as the search perspective when
# settings.periodic_graperank_pubkey is not set.
_DEFAULT_OBSERVER_PUBKEY = (
    "be7bf5de068c1d842ed34a7c270507ec940f5ea51671cfd062a95e9d09420d0a"
)
RESULTS_LIMIT = 400

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


def _observer_pubkey() -> str:
    return settings.periodic_graperank_pubkey or _DEFAULT_OBSERVER_PUBKEY


@router.get(
    path="/byText",
    summary="Search Nostr profiles by free-text, npub, or hex pubkey",
)
async def search_by_text_endpoint(
    text: str = Query(..., min_length=1, max_length=100),
    onlyRanked: bool = Query(
        default=True,
        description="If true, only return profiles that have a non-zero quality_score.",
    ),
    ownPubkey: bool = Query(
        default=False,
        description=(
            "If true and the caller is authenticated (JWT or NIP-98), use the "
            "caller's own pubkey as the observer perspective for trust scores. "
            "Defaults to the periodic graperank pubkey."
        ),
    ),
    jwt_data: Optional[JWTData] = Depends(verify_token_optional),
) -> SearchByTextResponse:
    sanitized = _sanitize(text)
    observer = _observer_pubkey()
    if ownPubkey:
        if jwt_data is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="ownPubkey=true requires a valid authentication token",
            )
        observer = jwt_data.nostr_pubkey

    pubkey = _try_resolve_pubkey(sanitized)
    if pubkey is not None:
        doc = await get_document(pubkey)
        results = [doc] if doc is not None else []
    else:
        # name_and_quality_score_only already orders by quality_boost-weighted
        # relevance, so we don't need a client-side rank-based re-sort.
        results = await search(
            query_text=sanitized,
            user_pubkey=observer,
            hits=RESULTS_LIMIT,
            include_zero_score_results=not onlyRanked,
        )

    return SearchByTextResponse(
        data=SearchResults(
            query=sanitized,
            numResults=len(results),
            results=results,
        )
    )
