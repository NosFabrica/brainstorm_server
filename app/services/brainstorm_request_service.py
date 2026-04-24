from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from app.core.redis_db import redis_client

from app.db_models import BrainstormRequest, BrainstormRequestStatus
from app.repos.brainstorm_nsec import (
    get_graperank_preset_by_pubkey_on_db,
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
    update_last_time_triggered_graperank_on_db,
)
from app.repos.brainstorm_request_repo import (
    count_brainstorm_requests_with_priority_over_one_on_db,
    create_brainstorm_request_on_db,
    delete_brainstorm_request_by_id_on_db,
    select_brainstorm_request_by_id_on_db,
)

from app.schemas.schemas import BrainstormRequestInstance
from app.services.graperank_presets import (
    build_params_payload,
    normalize_preset,
)


def brainstorm_request_db_obj_to_schema_converter(
    brainstorm_request_db_obj: BrainstormRequest,
    include_result: bool = False,
    how_many_others_with_priority: int = 0,
) -> BrainstormRequestInstance:
    brainstorm_request_obj = BrainstormRequestInstance(
        private_id=brainstorm_request_db_obj.private_id,
        status=brainstorm_request_db_obj.status,
        result=brainstorm_request_db_obj.result if include_result else None,
        password=brainstorm_request_db_obj.password,
        created_at=brainstorm_request_db_obj.created_at,
        updated_at=brainstorm_request_db_obj.updated_at,
        algorithm=brainstorm_request_db_obj.algorithm,
        parameters=brainstorm_request_db_obj.parameters,
        how_many_others_with_priority=how_many_others_with_priority,
        internal_publication_status=brainstorm_request_db_obj.status_internal_brainstorm_publication,
        ta_status=brainstorm_request_db_obj.status_ta_publication,
        pubkey=brainstorm_request_db_obj.pubkey,
        count_values=brainstorm_request_db_obj.count_values,
        graperank_preset_used=brainstorm_request_db_obj.graperank_preset_used,
    )

    return brainstorm_request_obj


async def get_brainstorm_request_by_id(
    db: AsyncDBSession,
    brainstorm_request_id: int,
    include_result: bool,
) -> BrainstormRequestInstance:
    brainstorm_request_db_obj = await select_brainstorm_request_by_id_on_db(
        db=db,
        brainstorm_request_id=brainstorm_request_id,
        include_result=include_result,
    )

    how_many_others_with_priority = (
        0
        if brainstorm_request_db_obj.status != BrainstormRequestStatus.WAITING.value
        else await count_brainstorm_requests_with_priority_over_one_on_db(
            db, brainstorm_request_db_obj.private_id
        )
    )

    return brainstorm_request_db_obj_to_schema_converter(
        brainstorm_request_db_obj=brainstorm_request_db_obj,
        include_result=include_result,
        how_many_others_with_priority=how_many_others_with_priority,
    )


async def delete_brainstorm_request_by_id(
    db: AsyncDBSession, brainstorm_request_id: int
) -> None:
    await delete_brainstorm_request_by_id_on_db(
        db=db, brainstorm_request_id=brainstorm_request_id
    )


# async def update_brainstorm_request_by_id(
#     db: AsyncDBSession,
#     brainstorm_request_id: int,
#     result: str,
#     status: BrainstormRequestStatus,
# ) -> None:

#     await update_brainstorm_request_duration_by_id_on_db(
#         db=db,
#         brainstorm_request_id=brainstorm_request_id,
#         result=result,
#         status=status,
#     )


async def create_brainstorm_request(
    db: AsyncDBSession,
    algorithm: str,
    parameters: str,
    pubkey: str,
    nsec_exists: bool = False,
) -> BrainstormRequestInstance:

    stored_preset = await get_graperank_preset_by_pubkey_on_db(db, parameters)
    preset = normalize_preset(stored_preset)

    brainstorm_request_db_obj: BrainstormRequest = (
        await create_brainstorm_request_on_db(
            db,
            algorithm=algorithm,
            parameters=parameters,
            pubkey=pubkey,
            graperank_preset_used=preset.value,
        )
    )

    how_many_others_with_priority = (
        await count_brainstorm_requests_with_priority_over_one_on_db(
            db, brainstorm_request_db_obj.private_id
        )
    )

    instance = brainstorm_request_db_obj_to_schema_converter(
        brainstorm_request_db_obj=brainstorm_request_db_obj,
        how_many_others_with_priority=how_many_others_with_priority,
    )
    instance.graperank_params = build_params_payload(preset)

    if not nsec_exists:
        await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
            db, pubkey=parameters
        )

    await update_last_time_triggered_graperank_on_db(db, parameters)

    await redis_client.rpush("message_queue", instance.model_dump_json())

    return instance
