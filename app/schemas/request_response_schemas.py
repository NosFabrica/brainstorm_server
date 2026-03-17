from typing import Any

from fastapi import status
from pydantic import BaseModel

from app.schemas.schemas import (
    AuthSuccessfulToken,
    BrainstormPubkeyInstance,
    BrainstormRequestInstance,
    OwnUserData,
    UserGraphData,
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
