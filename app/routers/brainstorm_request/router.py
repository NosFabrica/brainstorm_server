from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.schemas.request_body_schemas import CreateBrainstormRequestBody
from app.schemas.request_response_schemas import BrainstormRequestResponse
from app.schemas.schemas import BrainstormRequestInstance
from app.services.brainstorm_request_service import (
    create_brainstorm_request,
    get_brainstorm_request_by_id,
)


router = APIRouter()


@router.get(
    path="/{brainstorm_request_id}",
    tags=[],
    dependencies=[],
    summary="Get a Branstorm Request endpoint (admin only)",
)
async def get_brainstorm_request_endpoint(
    brainstorm_request_id: int,
    include_result: bool = Query(False),
    db: AsyncDBSession = Depends(dependency=get_db),
) -> BrainstormRequestResponse:

    result: BrainstormRequestInstance = await get_brainstorm_request_by_id(
        db=db,
        brainstorm_request_id=brainstorm_request_id,
        include_result=include_result,
        is_admin=True,
    )
    return BrainstormRequestResponse(data=result)


@router.post(
    path="/",
    tags=[],
    dependencies=[],
    summary="Create a Branstorm Request endpoint (admin only)",
)
async def create_brainstorm_request_endpoint(
    brainstorm_request_creation_body: CreateBrainstormRequestBody,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> BrainstormRequestResponse:

    result: BrainstormRequestInstance = await create_brainstorm_request(
        db=db,
        algorithm=brainstorm_request_creation_body.algorithm,
        parameters=brainstorm_request_creation_body.parameters,
        pubkey=brainstorm_request_creation_body.pubkey,
    )
    return BrainstormRequestResponse(data=result)
