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
