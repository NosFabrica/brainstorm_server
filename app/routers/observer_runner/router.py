"""`/internal/observerRunner/*`: the Observer print worker's calls.

INTERNAL ONLY. Served by the internal app (`app/internal_api.py`) on its own
port, `OBSERVER_RUNNER_PORT`, never by the public app, which answers 404 for
`/internal/` before routing. The worker runs beside this server on the
internal network and reaches that port directly. Rules and allow-lists are
`services/observer_runner_service.py`'s.

Auth is `Authorization: Bearer <OBSERVER_RUNNER_TOKEN>`, compared in constant
time by `guard.RequireRunnerToken` before routing: with the setting empty or
too short, or a missing or wrong token, every method and path answers the same
404 a path that doesn't exist answers (no 405, no redirect, no 422). The
dependency below checks it again, should the router ever be mounted without
the middleware. Bodies are parsed only after that. Kept out of the OpenAPI
document.
"""
from __future__ import annotations

from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.routers.observer_runner.guard import bearer_matches
from app.schemas.request_body_schemas import (
    ObserverRunnerDecryptBody,
    ObserverRunnerSignBody,
)
from app.schemas.request_response_schemas import (
    ObserverRunnerAssistantsResponse,
    ObserverRunnerPlaintextResponse,
    ObserverRunnerSignedResponse,
)
from app.services.observer_runner_service import (
    decrypt_as_assistant,
    list_assistants,
    sign_as_assistant,
)

MAX_PAGE = 500

M = TypeVar("M", bound=BaseModel)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


async def verify_observer_runner_token(request: Request) -> None:
    """404 unless the request carries the configured worker token."""
    if not bearer_matches(request.headers.get("authorization", "")):
        raise _not_found()


router = APIRouter(
    dependencies=[Depends(verify_observer_runner_token)], include_in_schema=False
)


async def _body(request: Request, model: type[M]) -> M:
    """The JSON body as `model`, read only once the token has passed."""
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as e:
        # `include_input=False`: the input can be a ciphertext.
        raise RequestValidationError(
            e.errors(include_input=False, include_url=False)
        ) from None


@router.get("/assistants")
async def list_assistants_endpoint(
    after: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    limit: int = Query(default=MAX_PAGE, ge=1, le=MAX_PAGE),
    db: AsyncDBSession = Depends(dependency=get_db),
) -> ObserverRunnerAssistantsResponse:
    return ObserverRunnerAssistantsResponse(
        data=await list_assistants(db, after=after, limit=limit)
    )


@router.post("/decrypt")
async def decrypt_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> ObserverRunnerPlaintextResponse:
    body = await _body(request, ObserverRunnerDecryptBody)
    return ObserverRunnerPlaintextResponse(
        data=await decrypt_as_assistant(db, body.owner, body.event, body.schedule)
    )


@router.post("/sign")
async def sign_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> ObserverRunnerSignedResponse:
    body = await _body(request, ObserverRunnerSignBody)
    event = body.event
    return ObserverRunnerSignedResponse(
        data=await sign_as_assistant(
            db, body.owner, event.kind, event.created_at, event.tags, event.content
        )
    )
