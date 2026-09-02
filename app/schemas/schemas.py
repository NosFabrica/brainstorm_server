from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)

from app.core.flash import is_whole_day_boundary
from app.schemas.error_codes import ErrorCode


def _instant_wire_format(value: datetime | None) -> str | None:
    # Stored naive UTC; without an explicit Z, `new Date()` in the browser reads
    # it as local time and shifts it by the viewer's offset.
    return None if value is None else value.isoformat() + "Z"


def _billing_date_wire_format(value: datetime | None) -> str | None:
    """A stored billing date, in the shape Flash named it.

    A boundary Flash gave as a bare date goes back out as that date — there is
    no time to localise, and localising the whole-day value we stood in for it
    can only move the day. Anything carrying a real time is an instant. The
    stored value is the only record of the shape, so a genuine instant landing
    exactly on a day boundary reads as a date: it costs a rendering, never a
    grant, and it stops happening as Flash moves to instants.
    """
    if value is not None and is_whole_day_boundary(value):
        return value.date().isoformat()
    return _instant_wire_format(value)


#################

# Specific data #

#################


class AuthSuccessfulToken(BaseModel):
    token: str


class FollowListIngestResult(BaseModel):
    followCount: int


##########################

# Business specific data #

##########################


class CreatedAndUpdatedAtModel(BaseModel):
    created_at: datetime
    updated_at: datetime


class GrapeRankError(BaseModel):
    code: ErrorCode
    message: str | None = None

    @model_validator(mode="before")
    @classmethod
    def bucket_unknown_code(cls, data):
        if isinstance(data, dict):
            raw_code = data.get("code")
            if (
                isinstance(raw_code, str)
                and raw_code not in ErrorCode._value2member_map_
            ):
                existing = data.get("message")
                data = {
                    **data,
                    "code": ErrorCode.UNKNOWN.value,
                    "message": f"{existing} ({raw_code})" if existing else None,
                }
        return data


class BrainstormRequestInstance(CreatedAndUpdatedAtModel):
    private_id: int
    status: str
    ta_status: str | None
    internal_publication_status: str | None
    count_values: str | None
    password: str
    algorithm: str
    parameters: str
    how_many_others_with_priority: int
    pubkey: str | None
    trigger_source: str | None = None
    graperank_preset_used: str | None = None
    graperank_params: dict | None = None
    error: GrapeRankError | None = None


class SchedulerStats(BaseModel):
    throughput_per_day: float
    demand_per_day: float
    median_publish_seconds: float | None
    lane_depths: dict[str, int]
    tier_slip_seconds: dict[str, float]


class AdminStats(BaseModel):
    total_users: int | None
    scored_users: int
    sp_adopters: int | None
    total_reports: int | None
    queue_depth: int


class AdminUserListItem(BaseModel):
    pubkey: str
    ta_pubkey: str | None
    times_calculated: int
    last_triggered: datetime
    last_updated: datetime
    latest_status: str | None
    latest_ta_status: str | None
    latest_algorithm: str | None
    scheduling_id: int | None
    scheduling_name: str


class AdminUserDetail(BaseModel):
    pubkey: str
    scheduling_id: int | None
    scheduling_name: str


class SchedulingItem(BaseModel):
    id: int
    name: str
    schedule_interval_seconds: int
    priority: int
    enabled: bool
    is_default: bool
    is_public: bool
    manual_quota_limit: int
    manual_quota_window_seconds: int

    model_config = {"from_attributes": True}


class SchedulingUserItem(BaseModel):
    pubkey: str
    last_time_published_graperank: datetime | None


class BrainstormPubkeyInstance(CreatedAndUpdatedAtModel):
    global_pubkey: str
    brainstorm_pubkey: str
    triggered_graperank: BrainstormRequestInstance | None


class UserConnection(BaseModel):
    pubkey: str
    influence: float | None = None
    trusted_reporters: int | None = None


class UserGraphData(BaseModel):
    followed_by: list[UserConnection]
    following: list[UserConnection]
    muted_by: list[UserConnection]
    muting: list[UserConnection]
    reported_by: list[UserConnection]
    reporting: list[UserConnection]
    influence: float | None


class UserHistoryInstance(CreatedAndUpdatedAtModel):
    pubkey: str
    ta_pubkey: str
    last_time_calculated_graperank: datetime | None
    last_time_triggered_graperank: datetime | None


class OwnUserData(BaseModel):
    graph: UserGraphData
    history: UserHistoryInstance


class UserConnectionCounts(BaseModel):
    followed_by: int
    following: int
    muted_by: int
    muting: int
    reported_by: int
    reporting: int


class UserOverviewData(BaseModel):
    pubkey: str
    influence: float | None
    # The subject's own bucket under the observer's saved preset, against the
    # follower cutoff. Names are `ConnectionTierCounts`'; verified is any banded
    # tier, so there's no separate flag.
    tier: str | None = None
    flagged_by_observer: bool
    flagged_count: int
    counts: UserConnectionCounts


class ConnectionTierCounts(BaseModel):
    """Bucket names match the GR result writer's `count_values` keys
    (message_queue_consumer.py) so a single mental model applies across
    /stats, /connections?tier=…, and the GR per-hop count_values."""

    high: int
    medium_high: int
    medium: int
    medium_low: int
    low: int
    low_and_reported_by_2_or_more_trusted_pubkeys: int


class ConnectionStats(BaseModel):
    total: int
    verified: int
    tier_counts: ConnectionTierCounts


class UserConnectionItem(BaseModel):
    pubkey: str
    influence: float | None = None
    trusted_reporters: int | None = None
    # The bucket `/stats` counted this row in, so a client renders the server's
    # verdict rather than deriving its own from a threshold. Same fallthrough
    # `/stats` and `?tier=` use. NB not the same question as `verified_only`,
    # which filters on the section's own cutoff (muter/reporter, not follower).
    tier: str | None = None


class PaginatedUserConnections(BaseModel):
    items: list[UserConnectionItem]
    next_cursor: str | None = None
    # Total count of items matching the current filter, independent of cursor.
    # Lets the client render a stable pager total ("page N of M") from page 1
    # rather than rederiving as more pages stream in. Opt-in: `None` unless the
    # request passed `with_total=true` (the count is a second graph scan).
    total: int | None = None


class UserSectionsStats(BaseModel):
    followed_by: ConnectionStats
    following: ConnectionStats
    muted_by: ConnectionStats
    muting: ConnectionStats
    reported_by: ConnectionStats
    reporting: ConnectionStats


class BillingSubscriptionItem(BaseModel):
    """One subscriber, with the two questions kept apart: `flash_status` is what
    Flash says we are charging them, `scheduling_name` is what the scheduler
    actually gives them. Disagreement between them is the bug."""

    pubkey: str
    flash_status: str
    # Flash's own id, so an operator can deep-link into the vault rather than
    # reading our copy of what it says.
    flash_subscription_id: str | None = None
    # The billing dates as Flash reports them. next_billing_date is what
    # answers "when is this person charged again" — without it an operator
    # cannot tell a renewal that is due from one that has silently stopped.
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    next_billing_date: datetime | None = None
    # Set means this subscription ends then, however `flash_status` reads —
    # under the account's end-of-period policy a cancelled subscriber stays
    # `active` until the date lands, so the column is the only thing that
    # separates "renews then" from "ends then".
    cancel_effective_date: datetime | None = None
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None
    granted_scheduling_id: int | None = None
    granted_scheduling_name: str | None = None
    scheduling_id: int | None = None
    scheduling_name: str | None = None
    scheduling_source: str
    billing_blocked: bool

    model_config = ConfigDict(from_attributes=True)

    @field_serializer(
        "current_period_start",
        "current_period_end",
        "next_billing_date",
        "cancel_effective_date",
    )
    def _serialize_billing_date(self, value: datetime | None) -> str | None:
        return _billing_date_wire_format(value)

    @field_serializer("last_synced_at")
    def _serialize_instant(self, value: datetime | None) -> str | None:
        # Ours, not Flash's, and always a real instant.
        return _instant_wire_format(value)


class DivergenceSection(BaseModel):
    """One kind of disagreement. `truncated` is explicit because a capped list
    that looks complete is worse than one that admits it isn't."""

    count: int
    truncated: bool
    rows: list[dict]


class SubscriptionPolicyView(BaseModel):
    """What the subscriber receives. This is their tier — there is no string.

    `is_default` is what "free" used to mean: the policy everyone holds without
    buying anything. The client compares nothing against a known name.
    """

    id: int
    name: str
    schedule_interval_seconds: int
    is_default: bool

    model_config = ConfigDict(from_attributes=True)


class SubscriptionPlanView(BaseModel):
    """Which plan this person bought, priced as they bought it.

    Which one is read through their billing row rather than looked up by
    policy: two plans can sell one policy, and quoting the wrong one quotes a
    price they are not charged. The price itself is the snapshot Flash recorded
    at signup, not the plan's current listing — repricing a plan must not
    rewrite what everyone already on it is told they pay. All three price
    fields are null when Flash recorded no snapshot; a page that cannot say the
    price must not therefore say the wrong one.

    `is_active` stays ours: false is how the UI tells them their plan is no
    longer offered, which is a decision Flash knows nothing about.

    No plan name: what a subscriber is shown is the POLICY they hold, which is
    what they actually receive. Carrying Flash's name here with nothing
    rendering it would be a field waiting for someone to invent a use for.

    `plan_id` is the exception, and it is an identifier rather than copy: it is
    the only way the pricing page can mark the row they actually bought when
    two plans grant one policy. Matched against `BillingPlanView.plan_id`,
    never interpreted.
    """

    plan_id: str
    amount_minor: int | None
    currency: str | None
    billing_interval: str | None
    is_active: bool


class SubscriptionView(BaseModel):
    """What the UI shows a signed-in user. Every field always present.

    No tier string and no `rail`: Flash's subscription object carries no
    payment-method field, and a permanently-null one reads as "unknown yet"
    rather than "unknowable". All three dates come straight off the row —
    nothing here is derived by date arithmetic.
    """

    # Null only on an instance with no scheduling policies at all, which is a
    # broken install rather than a state to render.
    policy: SubscriptionPolicyView | None
    plan: SubscriptionPlanView | None
    status: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    next_billing_date: datetime | None
    # Set once the subscriber has cancelled but the paid period is still
    # running. Flash reports that state as `active` with a date, so status
    # alone cannot distinguish "renews on the 1st" from "ends on the 1st" —
    # they are still entitled either way, which is why this is a field rather
    # than a status.
    cancel_effective_date: datetime | None
    manage_url: str | None

    @field_serializer(
        "current_period_start",
        "current_period_end",
        "next_billing_date",
        "cancel_effective_date",
    )
    def _serialize_billing_date(self, value: datetime | None) -> str | None:
        return _billing_date_wire_format(value)


class BillingPlanView(BaseModel):
    """One row of the pricing picker, rendered in the order it is returned.

    Two sources, and the split is the whole design. `policy_*` and `is_default`
    are ours — what buying this actually gets you, off the live `scheduling`
    row, so the page cannot advertise a cadence the scheduler is not running.
    Everything else is Flash's answer about the plan, so nothing here is a
    transcription anybody has to keep correct.

    Grouping key is `policy_id` and paid-vs-free is `is_default` — no
    vocabulary the client has to recognise. `checkout_url` is complete except
    `ref`, which the client appends; null on a row nobody can buy.
    """

    policy_id: int
    policy_name: str
    schedule_interval_seconds: int
    is_default: bool
    # Flash's plan id — how a subscriber's own row is told from the other plan
    # on the same policy. Same value in every environment, and matched against
    # `SubscriptionPlanView.plan_id` rather than recognised.
    plan_id: str | None
    # Null on the free row: nothing sells it, so there is no Flash plan to name
    # it, price it or say how often it charges.
    plan_name: str | None
    description: str | None
    amount_minor: int
    currency: str
    # Flash's own word — `daily`/`weekly`/`monthly`/`yearly`/`one_off` today.
    # Formatted by the client, never matched: their set can grow, and a period
    # we did not recognise must not take a purchasable plan off the page.
    billing_interval: str | None
    checkout_url: str | None
    features: list[str] | None
    not_included: list[str] | None


class BillingPlansData(BaseModel):
    plans: list[BillingPlanView]


class BillingPlanItem(CreatedAndUpdatedAtModel):
    """One plan mapping, as an operator sees it. Ids only — no secrets live here.

    Two decisions and nothing else, because two decisions is all a mapping is:
    which Flash plan grants which scheduling policy, and whether we sell it.
    Price, name, period, ordering and copy are read from Flash and were never
    ours to correct.
    """

    id: int
    flash_service_id: str
    flash_plan_id: str
    scheduling_id: int
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class CreateBillingPlanBody(BaseModel):
    # Extras refused, not ignored: a client still sending a price would
    # otherwise be told the mapping was created exactly as it asked.
    model_config = ConfigDict(extra="forbid")

    flash_service_id: str
    flash_plan_id: str
    scheduling_id: int
    is_active: bool = True


class UpdateBillingPlanBody(BaseModel):
    """Partial update; only the fields actually sent change.

    Dumped with `exclude_unset`, not `exclude_none`, so an explicit null is
    told apart from a field the form never touched.

    The Flash ids are accepted here but refused by the service once anyone has
    bought the mapping: a typo in a row nobody ever sold is a one-field fix,
    while rewriting the ids under a subscriber would retroactively change what
    they bought.
    """

    # See CreateBillingPlanBody: a price sent here is refused, not dropped.
    model_config = ConfigDict(extra="forbid")

    flash_service_id: str | None = Field(default=None, min_length=1)
    flash_plan_id: str | None = Field(default=None, min_length=1)
    scheduling_id: int | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _flash_ids_are_never_cleared(self) -> "UpdateBillingPlanBody":
        # `None` is the "not sent" default for every field here, so an explicit
        # null on a NOT NULL column has to be caught before `exclude_unset`
        # turns it into a write.
        for field in ("flash_service_id", "flash_plan_id"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be cleared")
        return self


class BillingBlockOutcome(BaseModel):
    pubkey: str
    blocked: bool
    revoked: bool


class CancelSubscriptionBody(BaseModel):
    """Why an operator cancelled, in their words. Optional, and passed to Flash
    verbatim so their record says the same thing ours does."""

    reason: str | None = Field(default=None, max_length=500)


class SetSubscriptionStatusBody(BaseModel):
    """Pause, or put back. Cancellation is its own endpoint, because it carries a
    reason, an effective date, and consequences a pause does not."""

    status: Literal["paused", "active"]


class BillingSubscriptionActionOutcome(BaseModel):
    """What Flash did, beside what we then did about it.

    `flash_status` is Flash's own answer after the write; `applied`/`reason` are
    the entitlement re-read that followed. Kept apart for the same reason the
    roster keeps its two columns apart.
    """

    pubkey: str
    subscription_id: str
    flash_status: str
    cancel_effective_date: datetime | None = None
    # The field to read after a cancellation, never `flash_status` — see
    # `flash.cancel_subscription` for why the status stays `active`.
    cancellation_scheduled: bool
    applied: bool
    # An `EntitlementReason`, or `reread_failed` when the write landed in Flash
    # and the re-read that follows it did not.
    reason: str

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("cancel_effective_date")
    def _serialize_billing_date(self, value: datetime | None) -> str | None:
        return _billing_date_wire_format(value)


class AttributeUnresolvedBody(BaseModel):
    """Who a signup that named nobody actually belongs to."""

    pubkey: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class UnresolvedResolutionOutcome(BaseModel):
    subscription_id: str
    # `attributed` or `dismissed` — the same vocabulary the event row now carries.
    resolution: str
    pubkey: str | None
    # Whether a scheduling policy was actually written. False for a dismissal,
    # and for an attribution to a blocked user or onto a lapsed subscription.
    applied: bool
    # The EntitlementReason behind `applied` — see app/routers/CLAUDE.md.
    entitlement_reason: str | None = None
    events_settled: int
