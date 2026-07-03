"""Pydantic request / response schemas for the Open Ranking endpoints
(ORE-02..ORE-07). Field names match the spec exactly — do not rename.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---- ORE-02: /stats/pubkey ----


class StatsPubkeyRequest(BaseModel):
    pubkey: str
    algorithm: str | None = None
    pov: str | None = None


class StatsPubkeyResponse(BaseModel):
    pubkey: str
    rank: float
    follows: int | None = None
    followers: int | None = None
    mutes: int | None = None
    muters: int | None = None
    reports: int | None = None
    reporters: int | None = None
    first_seen_at: int | None = None
    ttl: int | None = None


# ---- ORE-03: /rank/pubkeys ----


class RankPubkeysRequest(BaseModel):
    pubkeys: list[str]
    algorithm: str | None = None
    pov: str | None = None
    limit: int | None = None


class RankResult(BaseModel):
    pubkey: str
    rank: float


class RankPubkeysResponse(BaseModel):
    results: list[RankResult]
    ttl: int | None = None


# ---- ORE-05: /search/pubkeys ----


class SearchPubkeysRequest(BaseModel):
    query: str
    algorithm: str | None = None
    pov: str | None = None
    limit: int | None = None


class SearchPubkeysResponse(BaseModel):
    results: list[RankResult]
    ttl: int | None = None


# ---- ORE-06 / ORE-07: /followers and /muters ----


class FollowersOrMutersRequest(BaseModel):
    pubkey: str
    algorithm: str | None = None
    pov: str | None = None
    limit: int | None = None


class FollowersOrMutersResponse(BaseModel):
    results: list[RankResult]
    total: int | None = None
    ttl: int | None = None
