from datetime import datetime

from pydantic import BaseModel


#################

# Specific data #

#################


class AuthSuccessfulToken(BaseModel):
    token: str


##########################

# Business specific data #

##########################


class CreatedAndUpdatedAtModel(BaseModel):
    created_at: datetime
    updated_at: datetime


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


class AdminUsersListData(BaseModel):
    data: list[AdminUserListItem]
    total: int
    page: int
    limit: int


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
