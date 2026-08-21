from typing import ClassVar
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.schemas.graperank_schemas import GrapeRankPresetTemplate
from app.schemas.nostr_event import NostrEvent
from app.utils.nostr import to_hex_pubkey


class CreateBrainstormRequestBody(BaseModel):
    algorithm: str
    parameters: str
    pubkey: str


class SubmitNostrAuthChallengeBody(BaseModel):
    signed_event: dict


class SubmitFollowListBody(BaseModel):
    signed_event: NostrEvent


class GetTrustSignalsBody(BaseModel):
    pubkeys: list[str]


class SetGrapeRankPresetBody(BaseModel):
    preset: GrapeRankPresetTemplate


class RefreshSubscriptionBody(BaseModel):
    """The checkout redirect's `subscriptionId`, and nothing else it echoes.

    The redirect also carries a `ref`, deliberately not taken: the reference is
    the signed-in caller's pubkey, which the token already says. Absent entirely
    on a `pending` return, which Flash issues no id for.
    """

    # Flash ids are UUIDs; the bound only keeps something absurd out of a URL.
    subscription_id: str | None = Field(default=None, max_length=200)


class CreateShortUrlBody(BaseModel):
    # Relay hints ride in the share link so a visitor's client can resolve a
    # profile we haven't indexed. Seven is plenty and bounds the stored row.
    MAX_RELAYS: ClassVar[int] = 7

    pubkey: str = Field(
        description="Profile pubkey, hex or npub. Normalised to hex on the way in."
    )
    relays: list[str] = Field(
        max_length=MAX_RELAYS,
        description="Relay hints; each a ws:// or wss:// URL with a host.",
    )

    @field_validator("pubkey")
    @classmethod
    def _normalise_pubkey(cls, value: str) -> str:
        """Accept hex or npub, store hex. Shares `to_hex_pubkey` with the
        `resolve_pubkey_or_400` used by the other pubkey-taking endpoints."""
        return to_hex_pubkey(value.strip())

    @field_validator("relays")
    @classmethod
    def _relays_well_formed(cls, relays: list[str]) -> list[str]:
        """Validate and normalise together — returning the raw list would store
        and echo back surrounding whitespace the check already ignored."""
        cleaned = []
        for relay in relays:
            stripped = relay.strip()
            parsed = urlparse(stripped)
            if parsed.scheme not in ("ws", "wss") or not parsed.netloc:
                raise ValueError(f"{relay!r} is not a ws:// or wss:// URL with a host")
            cleaned.append(stripped)
        return cleaned


class SetUserSchedulingBody(BaseModel):
    scheduling_id: int


class CreateSchedulingBody(BaseModel):
    # Bounded because Swagger's "Try it out" sends the schema example verbatim,
    # and the router cannot tell that from a deliberate body. A 0 cadence makes
    # is_overdue always true, so everyone on the policy is recalculated forever.
    name: str = Field(min_length=1)
    schedule_interval_seconds: int = Field(gt=0)
    priority: int = 0
    enabled: bool = True
    is_default: bool = False
    # Whether this policy may appear on the public pricing page. `name` is what
    # the picker shows, so this form is where a tier is defined outright.
    is_public: bool = False
    manual_quota_limit: int = Field(default=20, ge=1)
    manual_quota_window_seconds: int = Field(default=604800, gt=0)


class BulkAssignSchedulingBody(BaseModel):
    pubkeys: list[str]


class UpdateSchedulingBody(BaseModel):
    """Partial: only supplied fields change. Same bounds as create."""

    name: str | None = Field(default=None, min_length=1)
    schedule_interval_seconds: int | None = Field(default=None, gt=0)
    priority: int | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    is_public: bool | None = None
    manual_quota_limit: int | None = Field(default=None, ge=1)
    manual_quota_window_seconds: int | None = Field(default=None, gt=0)
