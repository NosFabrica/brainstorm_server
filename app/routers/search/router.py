import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from nostr_sdk import PublicKey

from app.core.vespa import DEFAULT_MIN_RANK, SORT_PROFILES, get_document, search
from app.schemas.request_response_schemas import (
    SearchByTextResponse,
    SearchResults,
)
from app.utils.api_validators import verify_token_optional
from app.utils.auth.auth_models import JWTData
from app.utils.observer import default_observer_pubkey

router = APIRouter()

RESULTS_LIMIT = 400
RESULTS_DEFAULT = 100

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
    maxHits: int = Query(
        default=RESULTS_DEFAULT,
        gt=0,
        description=(
            "Maximum number of results to return. Capped at "
            f"{RESULTS_LIMIT}; larger values are clamped down."
        ),
    ),
    sort: str = Query(
        default="followers",
        description=(
            "Sort order: 'followers' (default — verified-follower count, "
            "popular-first), 'rank' (observer trust), or 'text' (pure text "
            "relevance). All apply the rank>=minRank filter. See "
            "docs/search-vs-tapestry.md §8.1."
        ),
    ),
    minRank: float = Query(
        default=DEFAULT_MIN_RANK,
        ge=0,
        description=(
            "Exclude profiles whose observer rank (influence*100) is below this. "
            f"Default {int(DEFAULT_MIN_RANK)}."
        ),
    ),
    jwt_data: Optional[JWTData] = Depends(verify_token_optional),
) -> SearchByTextResponse:
    sanitized = _sanitize(text)
    if sort not in SORT_PROFILES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"sort must be one of {sorted(SORT_PROFILES)}",
        )
    observer = default_observer_pubkey()
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
        # Default: text match -> filter rank>=minRank -> sort by verified
        # followers (docs/search-vs-tapestry.md §8.1). `sort` picks the profile;
        # the rank filter is applied via min_rank for every sort mode.
        results = await search(
            query_text=sanitized,
            user_pubkey=observer,
            hits=min(maxHits, RESULTS_LIMIT),
            include_zero_score_results=not onlyRanked,
            ranking_profile=SORT_PROFILES[sort],
            min_rank=minRank,
        )

    return SearchByTextResponse(
        data=SearchResults(
            query=sanitized,
            numResults=len(results),
            results=results,
        )
    )
