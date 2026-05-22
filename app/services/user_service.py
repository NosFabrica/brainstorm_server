import asyncio
import base64
import binascii
import json
import math
from typing import Literal

from fastapi import HTTPException, status

from app.core.redis_db import redis_client
from app.db_models import BrainstormNsec, BrainstormRequestStatus
from app.message_queue_tasks.process_strfry_event import (
    FOLLOWED_BY_KEY_PREFIX,
    MUTED_BY_KEY_PREFIX,
    REPORTED_BY_KEY_PREFIX,
)
from app.models.grapeRankResult import GrapeRankResult
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.brainstorm_nsec import select_brainstorm_nsec_history_fields_on_db
from app.repos.brainstorm_request_repo import (
    count_brainstorm_requests_with_priority_over_one_on_db,
    select_latest_brainstorm_request_on_db,
    select_latest_successful_brainstorm_request_on_db,
)
from app.repos.user_repo import (
    get_all_section_stats,
    get_outbound_counts_and_influence,
    get_paginated_section_connections,
    get_user_graph_data as _repo_get_user_graph_data,
)
from app.schemas.schemas import (
    BrainstormRequestInstance,
    PaginatedUserConnections,
    UserConnectionCounts,
    UserConnectionItem,
    UserGraphData,
    UserHistoryInstance,
    UserOverviewData,
    UserSectionsStats,
)
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from nostr_sdk import Keys
from app.services.brainstorm_request_service import (
    brainstorm_request_db_obj_to_schema_converter,
)

# Tier boundaries match the FE (high ≥ 0.5, trusted ≥ 0.2, neutral ≥ 0.07).
# The verified_threshold separates "low" from "unverified" — passed per request
# since it depends on the user's selected trust preset.
TIER_HIGH = 0.50
TIER_TRUSTED = 0.20
TIER_NEUTRAL = 0.07
DEFAULT_VERIFIED_THRESHOLD = 0.02

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50

ConnectionKind = Literal[
    "followed_by", "following", "muted_by", "muting", "reported_by", "reporting"
]

# (relationship_type, direction). direction "in" = other -[r]-> user, "out" = user -[r]-> other.
_KIND_TO_REL: dict[ConnectionKind, tuple[str, str]] = {
    "followed_by": ("FOLLOWS", "in"),
    "following": ("FOLLOWS", "out"),
    "muted_by": ("MUTES", "in"),
    "muting": ("MUTES", "out"),
    "reported_by": ("REPORTS", "in"),
    "reporting": ("REPORTS", "out"),
}


def _safe_float(value) -> float | None:
    # Neo4j can return inf/nan for stale or unbacked influence properties.
    # json.dumps rejects these, so coerce to None for the wire.
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isinf(f) or math.isnan(f):
        return None
    return f


def _encode_cursor(sort_inf: float, pubkey: str) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"i": sort_inf, "p": pubkey}).encode()
    ).decode()


def _decode_cursor(cursor: str) -> tuple[float, str]:
    payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    return float(payload["i"]), str(payload["p"])


async def _redis_inbound_count(prefix: str, pubkey: str) -> int:
    return int(await redis_client.scard(f"{prefix}{pubkey}"))


async def _neo4j_outbound_counts_and_influence(
    pubkey: str, influence_key: str
) -> tuple[float | None, int, int, int]:
    async with neo4j_driver.session() as session:
        influence, following, muting, reporting = (
            await get_outbound_counts_and_influence(session, pubkey, influence_key)
        )
    return _safe_float(influence), following, muting, reporting


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
    async with neo4j_driver.session() as session:
        return await _repo_get_user_graph_data(
            session, pubkey, influence_key, trusted_reporters_key
        )


async def get_user_history_data(db: AsyncDBSession, pubkey: str) -> UserHistoryInstance:
    brainstorm_nsec_db_obj = await select_brainstorm_nsec_history_fields_on_db(db, pubkey)
    return brainstorm_nsec_db_obj_to_user_history_schema_converter(
        brainstorm_nsec_db_obj=brainstorm_nsec_db_obj,
    )


async def get_user_overview(
    pubkey: str, observer: str | None = None
) -> UserOverviewData:
    influence_key = f"influence_{observer}" if observer else f"influence_{pubkey}"

    # Redis SCARD for inbound + one Neo4j query for outbound counts + influence,
    # all in parallel.
    followed_by, muted_by, reported_by, neo_result = await asyncio.gather(
        _redis_inbound_count(FOLLOWED_BY_KEY_PREFIX, pubkey),
        _redis_inbound_count(MUTED_BY_KEY_PREFIX, pubkey),
        _redis_inbound_count(REPORTED_BY_KEY_PREFIX, pubkey),
        _neo4j_outbound_counts_and_influence(pubkey, influence_key),
    )
    influence, following, muting, reporting = neo_result

    return UserOverviewData(
        pubkey=pubkey,
        influence=influence,
        counts=UserConnectionCounts(
            followed_by=followed_by,
            following=following,
            muted_by=muted_by,
            muting=muting,
            reported_by=reported_by,
            reporting=reporting,
        ),
    )


async def get_user_stats(
    pubkey: str,
    observer: str | None = None,
    verified_threshold: float = DEFAULT_VERIFIED_THRESHOLD,
    tier_high: float = TIER_HIGH,
    tier_trusted: float = TIER_TRUSTED,
    tier_neutral: float = TIER_NEUTRAL,
) -> UserSectionsStats:
    influence_key = f"influence_{observer}" if observer else f"influence_{pubkey}"

    async with neo4j_driver.session() as session:
        stats_by_kind = await get_all_section_stats(
            session,
            pubkey,
            influence_key,
            verified_threshold,
            tier_high,
            tier_trusted,
            tier_neutral,
        )
    return UserSectionsStats(**stats_by_kind)


async def get_user_connections(
    pubkey: str,
    observer: str | None = None,
    kind: ConnectionKind = "following",
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> PaginatedUserConnections:
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    rel_type, direction = _KIND_TO_REL[kind]
    influence_key = f"influence_{observer}" if observer else f"influence_{pubkey}"

    cursor_inf, cursor_pk = (None, None)
    if cursor:
        try:
            cursor_inf, cursor_pk = _decode_cursor(cursor)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, binascii.Error):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid cursor",
            )

    async with neo4j_driver.session() as session:
        items, last_cursor = await get_paginated_section_connections(
            session,
            pubkey=pubkey,
            influence_key=influence_key,
            rel_type=rel_type,
            direction=direction,
            limit=limit,
            cursor_inf=cursor_inf,
            cursor_pk=cursor_pk,
        )

    # Sanitize NaN/Inf floats post-query for safe JSON encoding.
    items = [
        UserConnectionItem(
            pubkey=it.pubkey,
            influence=_safe_float(it.influence),
        )
        for it in items
    ]

    next_cursor: str | None = None
    if last_cursor is not None:
        last_sort_inf = _safe_float(last_cursor[0])
        if last_sort_inf is not None:
            next_cursor = _encode_cursor(last_sort_inf, last_cursor[1])

    return PaginatedUserConnections(items=items, next_cursor=next_cursor)


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
