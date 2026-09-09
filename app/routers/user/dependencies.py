"""Shared FastAPI dependencies for the public `/user/{pubkey}/*` reads."""

from typing import Optional

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.services.verified_cutoffs import VerifiedCutoffs, resolve_verified_cutoffs
from app.utils.api_validators import verify_token_optional
from app.utils.auth.auth_models import JWTData
from app.utils.observer import default_observer_pubkey


def resolve_observer(jwt_data: Optional[JWTData]) -> str:
    """Observer perspective: the JWT viewer, else the platform default."""
    return jwt_data.nostr_pubkey if jwt_data else default_observer_pubkey()


async def get_verified_cutoffs(
    jwt_data: Optional[JWTData] = Depends(verify_token_optional),
    db: AsyncDBSession = Depends(get_db),
) -> VerifiedCutoffs:
    """The observer's saved-preset cutoffs — never a client-supplied threshold."""
    return await resolve_verified_cutoffs(db, resolve_observer(jwt_data))
