from typing import Any, TypedDict

# Plain dicts at runtime (parsed with orjson, no validation pass): we consume only
# what our own worker produces. These describe the shape for type checkers.


class ScoreCard(TypedDict, total=False):
    observer: str
    observee: str
    context: str
    averageScore: float
    input: float
    confidence: float
    influence: float
    verified: bool | None
    hops: int
    trusted_followers: int
    trusted_reporters: int
    trusted_muters: int  # absent from workers that predate it; read with .get


class GrapeRankResult(TypedDict, total=False):
    scorecards: dict[str, ScoreCard] | None
    rounds: int | None
    duration_seconds: float
    success: bool
    changedScorePubkeys: list[str]
    error: dict[str, Any] | None
