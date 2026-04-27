from typing import Any

from fastapi import status
from pydantic import BaseModel

from app.schemas.schemas import (
    AdminStats,
    AuthSuccessfulToken,
    BrainstormPubkeyInstance,
    BrainstormRequestInstance,
    OwnUserData,
    UserGraphData,
)
from app.schemas.graperank_schemas import (
    BuiltinPresetTemplate,
    GrapeRankPresetParams,
    GrapeRankPresetTemplate,
)


class BaseResponseDataSchema(BaseModel):
    code: int
    message: str | None = None
    data: Any


class ErrorDataSchema(BaseModel):
    error_message: str = "Internal Server Error"


class ErrorResponseSchema(BaseResponseDataSchema):
    code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    details: ErrorDataSchema | None


class SuccessfulResponseDataSchema(BaseResponseDataSchema):
    code: int = 200


# ACTUAL RESPONSES BELLOW


class NostrAuthChallenge(BaseModel):
    challenge: str


class BrainstormRequestResponse(SuccessfulResponseDataSchema):
    data: BrainstormRequestInstance


class BrainstormPubkeyResponse(SuccessfulResponseDataSchema):
    data: BrainstormPubkeyInstance


class NostrAuthChallengeResponse(SuccessfulResponseDataSchema):
    data: NostrAuthChallenge


class SubmitNostrAuthChallengeResponse(SuccessfulResponseDataSchema):
    data: AuthSuccessfulToken


class GetUserDataResponse(SuccessfulResponseDataSchema):
    data: UserGraphData


class GetOwnUserDataResponse(SuccessfulResponseDataSchema):
    data: OwnUserData


class GetOwnLatestGraperankResponse(SuccessfulResponseDataSchema):
    data: BrainstormRequestInstance | None


class WhitelistedPubkeys(BaseModel):
    observerPubkey: str
    numPubkeys: int
    pubkeys: list[str]


class GetWhitelistedPubkeysOfObserverResponse(SuccessfulResponseDataSchema):
    data: WhitelistedPubkeys


class PublishAssistantProfileData(BaseModel):
    event_id: str
    assistant_pubkey: str


class PublishAssistantProfileResponse(SuccessfulResponseDataSchema):
    data: PublishAssistantProfileData


class AdminStatsResponse(SuccessfulResponseDataSchema):
    data: AdminStats


class GrapeRankPreset(BaseModel):
    preset: GrapeRankPresetTemplate


class GrapeRankPresetResponse(SuccessfulResponseDataSchema):
    data: GrapeRankPreset


class GrapeRankPresetItem(BaseModel):
    id: GrapeRankPresetTemplate
    params: GrapeRankPresetParams


class GrapeRankPresetsData(BaseModel):
    presets: list[GrapeRankPresetItem]
    custom: GrapeRankPresetItem | None = None


class GrapeRankPresetsResponse(SuccessfulResponseDataSchema):
    data: GrapeRankPresetsData


# Admin-only schemas — typed with BuiltinPresetTemplate so OpenAPI docs don't
# expose CUSTOM as an option on admin endpoints.
class AdminPreset(BaseModel):
    preset: BuiltinPresetTemplate


class AdminPresetResponse(SuccessfulResponseDataSchema):
    data: AdminPreset


class AdminPresetItem(BaseModel):
    id: BuiltinPresetTemplate
    params: GrapeRankPresetParams


class AdminPresetItemResponse(SuccessfulResponseDataSchema):
    data: AdminPresetItem


class AdminPresetHistoryEntry(BaseModel):
    id: int
    presetId: BuiltinPresetTemplate
    params: GrapeRankPresetParams
    changeType: str
    changedBy: str | None
    changedAt: str


class AdminPresetHistoryData(BaseModel):
    entries: list[AdminPresetHistoryEntry]


class AdminPresetHistoryResponse(SuccessfulResponseDataSchema):
    data: AdminPresetHistoryData


