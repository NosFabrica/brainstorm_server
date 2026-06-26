"""Aggregator for the Open Ranking provider router.

Endpoints exposed (all root-mounted — the protocol mandates exact paths):

- GET  /.well-known/open-ranking.json   (ORE-01)
- POST /stats/pubkey                     (ORE-02)
- POST /rank/pubkeys                     (ORE-03)
- POST /search/pubkeys                   (ORE-05)
- POST /followers                        (ORE-06)
- POST /muters                           (ORE-07)

ORE-04 (recommendations) and ORE-08 (compromised pubkeys) are not yet
implemented and are intentionally absent from the capability document.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.routers.open_ranking.followers_muters import router as followers_muters_router
from app.routers.open_ranking.rank import router as rank_router
from app.routers.open_ranking.search import router as search_router
from app.routers.open_ranking.stats import router as stats_router
from app.routers.open_ranking.well_known import router as well_known_router
from app.utils.auth.nwt import verify_nwt


router = APIRouter()

# Capability document MUST be discoverable without auth (ORE-01).
router.include_router(well_known_router)

# Every data endpoint requires a valid NWT (ORE-A). The expected audience is
# derived from settings.public_base_url inside the verifier.
_auth_dep = [Depends(verify_nwt)]
router.include_router(stats_router, dependencies=_auth_dep)
router.include_router(rank_router, dependencies=_auth_dep)
router.include_router(search_router, dependencies=_auth_dep)
router.include_router(followers_muters_router, dependencies=_auth_dep)
