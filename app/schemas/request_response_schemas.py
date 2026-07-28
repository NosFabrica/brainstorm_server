from typing import Any

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.schemas import (
    AdminStats,
    AuthSuccessfulToken,
    BrainstormPubkeyInstance,
    BrainstormRequestInstance,
    FollowListIngestResult,
    OwnUserData,
    PaginatedUserConnections,
    UserGraphData,
    UserHistoryInstance,
    UserOverviewData,
    UserSectionsStats,
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


class SubmitFollowListResponse(SuccessfulResponseDataSchema):
    data: FollowListIngestResult


class GetUserDataResponse(SuccessfulResponseDataSchema):
    data: UserGraphData


class GetUserOverviewResponse(SuccessfulResponseDataSchema):
    data: UserOverviewData


class GetUserConnectionsResponse(SuccessfulResponseDataSchema):
    data: PaginatedUserConnections


class GetUserStatsResponse(SuccessfulResponseDataSchema):
    data: UserSectionsStats


class GetUserHistoryResponse(SuccessfulResponseDataSchema):
    data: UserHistoryInstance


class GetOwnUserDataResponse(SuccessfulResponseDataSchema):
    data: OwnUserData


class GetOwnLatestGraperankResponse(SuccessfulResponseDataSchema):
    data: BrainstormRequestInstance | None


class IsSearchObserverResponse(SuccessfulResponseDataSchema):
    data: bool


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


class SearchResults(BaseModel):
    query: str
    numResults: int
    results: list[dict]


class SearchByTextResponse(SuccessfulResponseDataSchema):
    data: SearchResults


class ShortestPathData(BaseModel):
    """Payload of GET /shortestPath (story shortest-path #1, ADR 0001).

    `from`/`to` are echoed as canonical hex (npub inputs are resolved), so
    they always match the pubkeys in `path`.
    """

    model_config = ConfigDict(populate_by_name=True)

    from_pubkey: str = Field(serialization_alias="from")
    to_pubkey: str = Field(serialization_alias="to")
    reachable: bool
    hops: int | None
    path: list[str] | None
    path_count: int = Field(serialization_alias="pathCount")
    path_count_capped: bool = Field(serialization_alias="pathCountCapped")
    max_hops: int = Field(serialization_alias="maxHops")


class GetShortestPathResponse(SuccessfulResponseDataSchema):
    data: ShortestPathData


class NetworkAlertItem(BaseModel):
    """One flagged pubkey in a network-alert section.

    Carries every trust score the panel needs so the front end doesn't have to
    follow up with /user/{pubkey}/overview per row. Kind-0 profile data is
    deliberately NOT here — the front end already resolves that separately.

    `verifiedReporterCount` and `reporterThreshold` are returned as a pair so
    the UI can explain *why* a row is flagged ("7 verified reports against a
    threshold of 4") rather than restating the rule client-side.
    """

    model_config = ConfigDict(populate_by_name=True)

    pubkey: str
    influence: float | None
    hops: int | None
    verified_follower_count: int = Field(serialization_alias="verifiedFollowerCount")
    verified_muter_count: int = Field(serialization_alias="verifiedMuterCount")
    verified_reporter_count: int = Field(serialization_alias="verifiedReporterCount")
    # The N this row was tested against: 2 + floor(verifiedFollowerCount / 500).
    reporter_threshold: int = Field(serialization_alias="reporterThreshold")


class NetworkAlertsData(BaseModel):
    """Payload of GET /networkAlerts.

    A pubkey qualifying for both sections appears only in `direct_follows` —
    the observer's own follow is the more actionable signal, and duplicating
    the row would double-count one bad actor in the panel.
    """

    model_config = ConfigDict(populate_by_name=True)

    observer_pubkey: str = Field(serialization_alias="observerPubkey")
    direct_follows: list[NetworkAlertItem] = Field(serialization_alias="directFollows")
    extended_network: list[NetworkAlertItem] = Field(
        serialization_alias="extendedNetwork"
    )
    # True when a section hit `limit` and more alerts exist behind it. Lets the
    # panel show "showing first N" instead of implying the list is exhaustive.
    direct_follows_truncated: bool = Field(serialization_alias="directFollowsTruncated")
    extended_network_truncated: bool = Field(
        serialization_alias="extendedNetworkTruncated"
    )


class GetNetworkAlertsResponse(SuccessfulResponseDataSchema):
    data: NetworkAlertsData
