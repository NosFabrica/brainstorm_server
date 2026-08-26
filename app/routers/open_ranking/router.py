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

from fastapi import APIRouter

from app.routers.open_ranking.followers_muters import router as followers_muters_router
from app.routers.open_ranking.rank import router as rank_router
from app.routers.open_ranking.search import router as search_router
from app.routers.open_ranking.stats import router as stats_router
from app.routers.open_ranking.well_known import router as well_known_router

router = APIRouter()

# Capability document MUST be discoverable without auth (ORE-01).
router.include_router(well_known_router)

# Auth posture is per-request and toggled by settings.open_ranking_require_auth:
# each data handler depends on `optional_nwt_signer`, which enforces the NWT
# (ORE-A) only when auth is enabled. See app/utils/auth/nwt.py.
router.include_router(stats_router)
router.include_router(rank_router)
router.include_router(search_router)
router.include_router(followers_muters_router)
