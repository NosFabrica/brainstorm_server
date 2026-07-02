from datetime import datetime

from pydantic import BaseModel, model_validator

from app.schemas.error_codes import ErrorCode


#################

# Specific data #

#################


class AuthSuccessfulToken(BaseModel):
    token: str


class FollowListIngestResult(BaseModel):
    followCount: int


##########################

# Business specific data #

##########################


class CreatedAndUpdatedAtModel(BaseModel):
    created_at: datetime
    updated_at: datetime


class GrapeRankError(BaseModel):
    code: ErrorCode
    message: str | None = None

    @model_validator(mode="before")
    @classmethod
    def bucket_unknown_code(cls, data):
        if isinstance(data, dict):
            raw_code = data.get("code")
            if (
                isinstance(raw_code, str)
                and raw_code not in ErrorCode._value2member_map_
            ):
                existing = data.get("message")
                data = {
                    **data,
                    "code": ErrorCode.UNKNOWN.value,
                    "message": f"{existing} ({raw_code})" if existing else None,
                }
        return data


class BrainstormRequestInstance(CreatedAndUpdatedAtModel):
    private_id: int
    status: str
    ta_status: str | None
    internal_publication_status: str | None
    result: str | None
    count_values: str | None
    password: str
    algorithm: str
    parameters: str
    how_many_others_with_priority: int
    pubkey: str | None
    graperank_preset_used: str | None = None
    graperank_params: dict | None = None
    error: GrapeRankError | None = None


class SchedulerStats(BaseModel):
    throughput_per_day: float
    demand_per_day: float
    median_publish_seconds: float | None
    lane_depths: dict[str, int]
    tier_slip_seconds: dict[str, float]


class AdminStats(BaseModel):
    total_users: int | None
    scored_users: int
    sp_adopters: int | None
    total_reports: int | None
    queue_depth: int


class AdminUserListItem(BaseModel):
    pubkey: str
    ta_pubkey: str | None
    times_calculated: int
    last_triggered: datetime
    last_updated: datetime
    latest_status: str | None
    latest_ta_status: str | None
    latest_algorithm: str | None
    scheduling_id: int | None
    scheduling_name: str


class AdminUserDetail(BaseModel):
    pubkey: str
    scheduling_id: int | None
    scheduling_name: str


class SchedulingItem(BaseModel):
    id: int
    name: str
    schedule_interval_seconds: int
    priority: int
    enabled: bool
    is_default: bool
    manual_quota_limit: int
    manual_quota_window_seconds: int

    model_config = {"from_attributes": True}


class SchedulingUserItem(BaseModel):
    pubkey: str
    last_time_published_graperank: datetime | None


class BrainstormPubkeyInstance(CreatedAndUpdatedAtModel):
    global_pubkey: str
    brainstorm_pubkey: str
    triggered_graperank: BrainstormRequestInstance | None


class UserConnection(BaseModel):
    pubkey: str
    influence: float | None = None
    trusted_reporters: int | None = None


class UserGraphData(BaseModel):
    followed_by: list[UserConnection]
    following: list[UserConnection]
    muted_by: list[UserConnection]
    muting: list[UserConnection]
    reported_by: list[UserConnection]
    reporting: list[UserConnection]
    influence: float | None


class UserHistoryInstance(CreatedAndUpdatedAtModel):
    pubkey: str
    ta_pubkey: str
    last_time_calculated_graperank: datetime | None
    last_time_triggered_graperank: datetime | None


class OwnUserData(BaseModel):
    graph: UserGraphData
    history: UserHistoryInstance


class UserConnectionCounts(BaseModel):
    followed_by: int
    following: int
    muted_by: int
    muting: int
    reported_by: int
    reporting: int


class UserOverviewData(BaseModel):
    pubkey: str
    influence: float | None
    flagged_by_observer: bool
    flagged_count: int
    counts: UserConnectionCounts


class ConnectionTierCounts(BaseModel):
    """Bucket names match the GR result writer's `count_values` keys
    (message_queue_consumer.py) so a single mental model applies across
    /stats, /connections?tier=…, and the GR per-hop count_values."""

    high: int
    medium_high: int
    medium: int
    medium_low: int
    low: int
    low_and_reported_by_2_or_more_trusted_pubkeys: int


class ConnectionStats(BaseModel):
    total: int
    verified: int
    tier_counts: ConnectionTierCounts


class UserConnectionItem(BaseModel):
    pubkey: str
    influence: float | None = None
    trusted_reporters: int | None = None


class PaginatedUserConnections(BaseModel):
    items: list[UserConnectionItem]
    next_cursor: str | None = None
    # Total count of items matching the current filter, independent of cursor.
    # Lets the client render a stable pager total ("page N of M") from page 1
    # rather than rederiving as more pages stream in. Opt-in: `None` unless the
    # request passed `with_total=true` (the count is a second graph scan).
    total: int | None = None


class UserSectionsStats(BaseModel):
    followed_by: ConnectionStats
    following: ConnectionStats
    muted_by: ConnectionStats
    muting: ConnectionStats
    reported_by: ConnectionStats
    reporting: ConnectionStats
