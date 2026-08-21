from pydantic import BaseModel

from app.schemas.graperank_schemas import GrapeRankPresetTemplate
from app.schemas.nostr_event import NostrEvent


class CreateBrainstormRequestBody(BaseModel):
    algorithm: str
    parameters: str
    pubkey: str


class SubmitNostrAuthChallengeBody(BaseModel):
    signed_event: dict


class SubmitFollowListBody(BaseModel):
    signed_event: NostrEvent


class SetGrapeRankPresetBody(BaseModel):
    preset: GrapeRankPresetTemplate


class CreateShortUrlBody(BaseModel):
    pubkey: str
    relays: list[str]


class SetUserSchedulingBody(BaseModel):
    scheduling_id: int


class CreateSchedulingBody(BaseModel):
    name: str
    schedule_interval_seconds: int
    priority: int = 0
    enabled: bool = True
    is_default: bool = False
    manual_quota_limit: int = 20
    manual_quota_window_seconds: int = 604800


class BulkAssignSchedulingBody(BaseModel):
    pubkeys: list[str]


class UpdateSchedulingBody(BaseModel):
    name: str | None = None
    schedule_interval_seconds: int | None = None
    priority: int | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    manual_quota_limit: int | None = None
    manual_quota_window_seconds: int | None = None
