from pydantic import BaseModel


class CreateBrainstormRequestBody(BaseModel):
    algorithm: str
    parameters: str
    pubkey: str


class SubmitNostrAuthChallengeBody(BaseModel):
    signed_event: dict


class SetGrapeRankPresetBody(BaseModel):
    preset: str
