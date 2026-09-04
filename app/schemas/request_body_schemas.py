from pydantic import BaseModel, Field

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


class RefreshSubscriptionBody(BaseModel):
    """The checkout redirect's `subscriptionId`, and nothing else it echoes.

    The redirect also carries a `ref`, deliberately not taken: the reference is
    the signed-in caller's pubkey, which the token already says. Absent entirely
    on a `pending` return, which Flash issues no id for.
    """

    # Flash ids are UUIDs; the bound only keeps something absurd out of a URL.
    subscription_id: str | None = Field(default=None, max_length=200)


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
