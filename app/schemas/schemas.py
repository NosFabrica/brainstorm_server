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


class BrainstormPubkeyInstance(CreatedAndUpdatedAtModel):
    global_pubkey: str
    brainstorm_pubkey: str
    triggered_graperank: BrainstormRequestInstance | None


class UserConnection(BaseModel):
    pubkey: str
    influence: float | None = None


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
