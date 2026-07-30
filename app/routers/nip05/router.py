"""Aggregator for the NIP-05 router.

Endpoints exposed (root-mounted — NIP-05 mandates the exact path):

- GET /.well-known/nostr.json
"""

from fastapi import APIRouter

from app.routers.nip05.well_known import router as well_known_router

router = APIRouter()

router.include_router(well_known_router)
