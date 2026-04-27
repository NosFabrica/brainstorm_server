from pydantic import BaseModel

from app.services.graperank_presets import BuiltinPresetTemplate


class CreateBrainstormRequestBody(BaseModel):
    algorithm: str
    parameters: str
    pubkey: str


class SubmitNostrAuthChallengeBody(BaseModel):
    signed_event: dict


class SetGrapeRankPresetBody(BaseModel):
    preset: BuiltinPresetTemplate
