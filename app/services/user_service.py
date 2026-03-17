from app.db_models import BrainstormNsec, BrainstormRequestStatus
from app.models.grapeRankResult import GrapeRankResult
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.brainstorm_nsec import select_brainstorm_nsec_by_pubkey_on_db
from app.repos.brainstorm_request_repo import (
    count_brainstorm_requests_with_priority_over_one_on_db,
    select_latest_brainstorm_request_on_db,
    select_latest_successful_brainstorm_request_on_db,
)
from app.repos.user_repo import (
    get_influence_for_observer,
    get_list_of_pubkeys_following_user,
    get_list_of_pubkeys_that_user_follows,
    get_list_of_pubkeys_that_user_mutes,
    get_list_of_pubkeys_muting_user,
    get_list_of_pubkeys_reporting_user,
    get_list_of_pubkeys_that_user_reports,
)
from app.schemas.schemas import (
    BrainstormRequestInstance,
    UserConnection,
    UserGraphData,
    UserHistoryInstance,
)
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from nostr_sdk import Keys
from app.services.brainstorm_request_service import (
    brainstorm_request_db_obj_to_schema_converter,
)


def brainstorm_nsec_db_obj_to_user_history_schema_converter(
    brainstorm_nsec_db_obj: BrainstormNsec,
) -> UserHistoryInstance:
    brainstorm_request_obj = UserHistoryInstance(
        pubkey=brainstorm_nsec_db_obj.pubkey,
        ta_pubkey=Keys.parse(secret_key=brainstorm_nsec_db_obj.nsec)
        .public_key()
        .to_hex(),
        last_time_calculated_graperank=brainstorm_nsec_db_obj.last_time_calculated_graperank,
        last_time_triggered_graperank=brainstorm_nsec_db_obj.last_time_triggered_graperank,
        created_at=brainstorm_nsec_db_obj.created_at,
        updated_at=brainstorm_nsec_db_obj.updated_at,
    )

    return brainstorm_request_obj


async def get_own_latest_graperank(
    db: AsyncDBSession, pubkey: str
) -> BrainstormRequestInstance | None:

    db_obj = await select_latest_brainstorm_request_on_db(db, pubkey)
    if not db_obj:
        return None

    how_many_others_with_priority = (
        0
        if db_obj.status != BrainstormRequestStatus.WAITING.value
        else await count_brainstorm_requests_with_priority_over_one_on_db(
            db, db_obj.private_id
        )
    )

    return brainstorm_request_db_obj_to_schema_converter(
        brainstorm_request_db_obj=db_obj,
        how_many_others_with_priority=how_many_others_with_priority,
    )


async def get_user_graph_data(
    pubkey: str,
    observer: str | None = None,
) -> UserGraphData:

    influence_key = f"influence_{observer}" if observer else f"influence_{pubkey}"
    trusted_reporters_key = (
        f"trusted_reporters_{observer}" if observer else f"trusted_reporters_{pubkey}"
    )

    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})

    CALL (user) {
        MATCH (other:NostrUser)-[:FOLLOWS]->(user)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS followed_by
    }

    CALL (user) {
        MATCH (user)-[:FOLLOWS]->(other:NostrUser)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS following
    }

    CALL (user) {
        MATCH (other:NostrUser)-[:MUTES]->(user)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS muted_by
    }

    CALL (user) {
        MATCH (user)-[:MUTES]->(other:NostrUser)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS muting
    }

    CALL (user) {
        MATCH (other:NostrUser)-[:REPORTS]->(user)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS reported_by
    }

    CALL (user) {
        MATCH (user)-[:REPORTS]->(other:NostrUser)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS reporting
    }

    RETURN
        user[$influence_key] AS influence,
        followed_by,
        following,
        muted_by,
        muting,
        reported_by,
        reporting
    """

    async with neo4j_driver.session() as session:
        result = await session.run(
            query,
            pubkey=pubkey,
            influence_key=influence_key,
            trusted_reporters_key=trusted_reporters_key,
        )

        record = await result.single()

    if not record:
        return UserGraphData(
            influence=None,
            followed_by=[],
            following=[],
            muted_by=[],
            muting=[],
            reported_by=[],
            reporting=[],
        )

    return UserGraphData(
        influence=record["influence"],
        followed_by=[UserConnection(**x) for x in record["followed_by"]],
        following=[UserConnection(**x) for x in record["following"]],
        muted_by=[UserConnection(**x) for x in record["muted_by"]],
        muting=[UserConnection(**x) for x in record["muting"]],
        reported_by=[UserConnection(**x) for x in record["reported_by"]],
        reporting=[UserConnection(**x) for x in record["reporting"]],
    )


async def get_user_graph_data_old(
    pubkey: str, observer: str | None = None
) -> UserGraphData:

    async with neo4j_driver.session() as neo4j_session:
        followed_by = await get_list_of_pubkeys_following_user(
            neo4j_session, pubkey, observer
        )
        following = await get_list_of_pubkeys_that_user_follows(
            neo4j_session, pubkey, observer
        )
        muted_by = await get_list_of_pubkeys_muting_user(
            neo4j_session, pubkey, observer
        )
        muting = await get_list_of_pubkeys_that_user_mutes(
            neo4j_session, pubkey, observer
        )
        reported_by = await get_list_of_pubkeys_reporting_user(
            neo4j_session, pubkey, observer
        )
        reporting = await get_list_of_pubkeys_that_user_reports(
            neo4j_session, pubkey, observer
        )
        influence = (
            1
            if observer is None
            else await get_influence_for_observer(neo4j_session, pubkey, observer)
        )

    return UserGraphData(
        influence=influence,
        followed_by=followed_by,
        following=following,
        muted_by=muted_by,
        muting=muting,
        reported_by=reported_by,
        reporting=reporting,
    )


async def get_user_history_data(db: AsyncDBSession, pubkey: str) -> UserHistoryInstance:

    brainstorm_nsec_db_obj = await select_brainstorm_nsec_by_pubkey_on_db(db, pubkey)

    return brainstorm_nsec_db_obj_to_user_history_schema_converter(
        brainstorm_nsec_db_obj=brainstorm_nsec_db_obj,
    )


async def get_whitelisted_pubkeys_of_observer(
    db: AsyncDBSession, pubkey: str, threshold: float = 0.02
) -> list[str]:

    latest_successful_result = await select_latest_successful_brainstorm_request_on_db(
        db, pubkey
    )

    if not latest_successful_result:
        return []

    if not latest_successful_result.result:
        raise Exception("successful graperank didnt have result")

    graperank_result = GrapeRankResult.model_validate_json(
        latest_successful_result.result
    )

    if graperank_result.scorecards is None:
        raise Exception("graperank_result.scorecards is None")

    return [
        x.observee
        for x in graperank_result.scorecards.values()
        if x.influence >= threshold
    ]
