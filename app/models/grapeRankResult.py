from pydantic import BaseModel


class ScoreCard(BaseModel):
    observer: str
    observee: str
    context: str = "not a bot"
    average_score: float = 0
    input: float | None = 0
    confidence: float = 0
    influence: float = 0
    verified: bool | None = None
    hops: int = 0
    trusted_followers: int = 0
    trusted_reporters: int = 0


class GrapeRankResult(BaseModel):
    scorecards: dict[str, ScoreCard] | None = None
    rounds: int | None = None
    duration_seconds: float
    success: bool = False
    changedScorePubkeys: list[str] = []
    droppedBelowCutoffPubkeys: list[str] = []
