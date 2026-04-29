from pydantic import BaseModel

from app.schemas.graperank_schemas import GrapeRankPresetTemplate


class CreateBrainstormRequestBody(BaseModel):
    algorithm: str
    parameters: str
    pubkey: str


class SubmitNostrAuthChallengeBody(BaseModel):
    signed_event: dict


class SetGrapeRankPresetBody(BaseModel):
    preset: GrapeRankPresetTemplate
