import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils.auth.auth_util import generate_secure_password


class BrainstormRequestStatus(enum.Enum):
    WAITING = "waiting"
    ONGOING = "ongoing"
    SUCCESS = "success"
    FAILURE = "failure"


class SchedulingSource(enum.Enum):
    """Who put a user on their scheduling policy. Billing declines to overrule ADMIN."""

    DEFAULT = "default"
    BILLING = "billing"
    ADMIN = "admin"


class TriggerSource(enum.Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    ADMIN = "admin"
    PERIODIC = "periodic"


class Base(DeclarativeBase, AsyncAttrs):
    pass


class TimestampMixin(object):
    @declared_attr
    def __tablename__(cls):
        return cls.__name__.lower()

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BrainstormRequest(TimestampMixin, Base):
    __tablename__ = "brainstorm_request"
    private_id: Mapped[int] = mapped_column(primary_key=True)
    password: Mapped[str] = mapped_column(
        String(128),
        default=generate_secure_password,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(128),
        default=BrainstormRequestStatus.WAITING.value,
        server_default=BrainstormRequestStatus.WAITING.value,
    )
    status_ta_publication: Mapped[str] = mapped_column(
        String(128),
        default=BrainstormRequestStatus.WAITING.value,
        server_default=BrainstormRequestStatus.WAITING.value,
    )
    status_internal_brainstorm_publication: Mapped[str] = mapped_column(
        String(128),
        default=BrainstormRequestStatus.WAITING.value,
        server_default=BrainstormRequestStatus.WAITING.value,
        nullable=True,
    )
    count_values: Mapped[str] = mapped_column(String, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    parameters: Mapped[str] = mapped_column(String, nullable=False)
    algorithm: Mapped[str] = mapped_column(String, nullable=False)
    pubkey: Mapped[str] = mapped_column(String, nullable=True)
    graperank_preset_used: Mapped[str] = mapped_column(String, nullable=True)
    graperank_params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Wall-clock seconds to publish this run's TAs (set on publish success).
    # Feeds the scheduler's measured median publish duration.
    publish_duration_seconds: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    # manual/scheduled/admin/periodic — drives priority-lane routing.
    trigger_source: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        server_default=TriggerSource.MANUAL.value,
        default=TriggerSource.MANUAL.value,
    )
    # Per-run, per-sink "force a full re-assert" overrides (published-state-drift
    # repair). Nullable: NULL/false = no override (delta as usual); true = this
    # run re-asserts that sink's full above-cutoff state. Set by the admin resync
    # endpoint and the every-Nth scheduled backstop.
    force_full_relay: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, server_default="false"
    )
    force_full_vespa: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, server_default="false"
    )


class BrainstormNostrRelayTransfer(TimestampMixin, Base):
    __tablename__ = "brainstorm_nostr_relay_transfer"
    private_id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    oldest: Mapped[int] = mapped_column(Integer, nullable=True)
    events: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[float] = mapped_column(Float, default=0)

    __table_args__ = (
        UniqueConstraint("kind", name="uq_brainstorm_nostr_relay_transfer_kind"),
    )


class BrainstormNsec(TimestampMixin, Base):
    __tablename__ = "brainstorm_nsec"
    nsec: Mapped[str] = mapped_column(String, nullable=False)
    encrypted_nsec: Mapped[str] = mapped_column(String, nullable=True)
    pubkey: Mapped[str] = mapped_column(String, primary_key=True)
    last_time_triggered_graperank = mapped_column(DateTime, nullable=True)
    last_time_calculated_graperank = mapped_column(DateTime, nullable=True)
    # Freshness clock: set only on a successful TA publish. Drives scheduling.
    last_time_published_graperank = mapped_column(DateTime, nullable=True)
    # Set once the Assistant's kind-0 profile has been published (see
    # assistant_profile_service). Null = never published: the TA upload task
    # publishes it best-effort before the observer's first TA batch and sets
    # this, so scores are never authored by a profile-less key.
    assistant_kind0_published_at = mapped_column(DateTime, nullable=True)
    graperank_preset: Mapped[str] = mapped_column(String, nullable=True)
    graperank_custom_params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_published_pubkeys: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    last_published_graperank_request_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("brainstorm_request.private_id"),
        nullable=True,
    )
    is_observer_search_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Scheduled deltas published since this observer's last successful full run.
    # Drives the every-Nth full backstop (FULL_SYNC_EVERY_N_RUNS). Counts
    # scheduled runs only; reset to 0 after a successful full run.
    runs_since_full: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    # The scheduling policy this user is assigned to (see the Scheduling table).
    # NULL = the default policy (is_default row). Assigned by admins / a future
    # service; NULL avoids any backfill for pre-existing users.
    scheduling_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("scheduling.id"),
        nullable=True,
    )
    # Barred from paid entitlement regardless of payment. Deliberately separate
    # from scheduling_source: "blocked", "comped" and "billing-controlled" are
    # three different states and must stay distinguishable. A blocked user is
    # still charged until they cancel, so cancellation must never be gated on it.
    billing_blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # See SchedulingSource. Billing only overwrites when this isn't ADMIN, so a
    # comped user survives a lapse and stays distinguishable from a bug.
    scheduling_source: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default=SchedulingSource.DEFAULT.value,
        default=SchedulingSource.DEFAULT.value,
    )


# Scheduling policies ("tiers"). DB-driven so policies (and their config) can be
# added/renamed/retuned without a code change — full admin CRUD later, or an
# external service writing rows. Referenced by BrainstormNsec.scheduling_id.
# The interactive lanes (Admin / Manual / House) are hardcoded and higher
# priority than anything here — they never live in this table.
class Scheduling(TimestampMixin, Base):
    __tablename__ = "scheduling"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # How often a user on this policy is recalculated (consumed by the
    # scheduler, issue 03). Stored in seconds for uniform, sub-day granularity.
    schedule_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    # Scheduling priority; higher is served first. Policies sharing a priority
    # share a lane (issue 02/03 routing).
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    # Per-policy on/off. Disabled = users on this policy are not auto-scheduled
    # (honored by the scheduler in issue 03); admins can pause a policy without
    # deleting it. Separate from the global SCHEDULER_ENABLED kill-switch.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    # Per-tier manual-recalc quota: at most `limit` successfully-published manual
    # runs per rolling `window` (issue 04). Default 20 / 7 days.
    manual_quota_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="20", default=20
    )
    manual_quota_window_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="604800", default=604800
    )
    # Exactly one row is the default, used for users with no explicit assignment.
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Whether this policy may reach /billing/plans. Off by default so an
    # internal policy cannot leak onto a public pricing page by being created.
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    __table_args__ = (
        # At most one default policy: partial unique index over the truthy rows.
        Index(
            "uq_scheduling_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )


# What a Flash plan grants. Data rather than config: dev and production are
# separate vaults with different UUIDs, so the mapping travels with the database.
class BillingPlan(TimestampMixin, Base):
    __tablename__ = "billing_plan"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flash_service_id: Mapped[str] = mapped_column(String, nullable=False)
    flash_plan_id: Mapped[str] = mapped_column(String, nullable=False)
    # The policy this plan grants. It IS the tier: several plans may point at
    # one policy (monthly beside yearly, a replacement beside the row it
    # retires) and all of them grant identically.
    scheduling_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scheduling.id"), nullable=False
    )
    # Minor units, as Flash sends them: 200 = $2.00. Integers throughout.
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    # How often Flash charges, as a unit and a count rather than a matched
    # string: the client formats "every 2 weeks" from the pair and an
    # unrecognised unit still renders. Both null on a plan whose period we have
    # not transcribed; "once" with a null count is reserved for Flash's coming
    # one-off type, which sells but grants nothing automatically.
    billing_period_unit: Mapped[str | None] = mapped_column(String, nullable=True)
    billing_period_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Display order in the picker. Not `scheduling.priority` — that is the
    # scheduler's queue lane, and it cannot order two plans inside one policy.
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    # Admin-editable plan copy, shaped like the includes/excludes Flash is
    # adding. Plain text only — stored markup on a public page is stored XSS.
    blurb: Mapped[str | None] = mapped_column(String, nullable=True)
    includes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    excludes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Sellable, and nothing else. Never filtered in the entitlement lookup.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )

    __table_args__ = (
        UniqueConstraint("flash_service_id", "flash_plan_id", name="uq_billing_plan_flash_ids"),
    )


# Why a user is on the tier they're on. Never consulted to decide whether they
# are paid — that is the scheduling assignment, and Flash's API is the authority.
class UserSubscription(TimestampMixin, Base):
    __tablename__ = "user_subscription"
    pubkey: Mapped[str] = mapped_column(String, primary_key=True)
    flash_subscription_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    flash_subscriber_id: Mapped[str | None] = mapped_column(String, nullable=True)
    billing_plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("billing_plan.id"), nullable=False
    )
    # What we actually granted, distinct from billing_plan.scheduling_id (the
    # rule). They diverge the moment a plan is retuned; revocation removes what
    # was granted, and the divergence report compares this against the live
    # assignment. NULL = recorded but nothing granted.
    granted_scheduling_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scheduling.id"), nullable=True
    )
    # Flash's status verbatim, unvalidated: their set is documented as open, so
    # an unrecognised value must land here intact rather than be coerced.
    flash_status: Mapped[str] = mapped_column(String, nullable=False)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_billing_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trial_end_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_effective_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Kept, but off the wire: Flash's subscription object has no payment-method
    # field, so this is structurally always null. Re-exposing it is one line the
    # day they publish one; inferring it is how we'd invent a payment method.
    rail: Mapped[str | None] = mapped_column(String, nullable=True)
    # Newest event timestamp seen for this subscriber; never moves backwards.
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(String, nullable=True)
    # When the *current* error first appeared. `last_synced_at` moves on every
    # attempt and `updated_at` on every write, so neither can answer "how long
    # has this been failing" — which is the only question that separates a
    # blip from something that will never resolve. Cleared by a successful read.
    sync_error_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# Inbound Flash webhooks. An inbox, not a ledger — never read to decide whether
# someone is paid; that comes from Flash's API. It collapses Flash's retries,
# recovers events we acknowledged then dropped, and preserves statuses we don't
# yet map. Rows are committed before the 200, because Flash stops retrying after
# a few attempts and never replays.
class FlashWebhookEvent(TimestampMixin, Base):
    __tablename__ = "flash_webhook_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Unconstrained: unrecognised events are recorded, never rejected.
    event: Mapped[str] = mapped_column(String, nullable=False)
    # When the event happened, per Flash's body. The ordering signal.
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When Flash *attempted delivery*, from the signature header. A retry of an
    # old event carries a newer value, so this orders nothing — audit only.
    delivery_timestamp: Mapped[int] = mapped_column(Integer, nullable=False)
    subscription_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    # Nullable only because a row can predate the payload column; Flash's
    # deliveries carry no personal data, so nothing here expires.
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Claimed by a worker, so the recovery sweep can tell "in progress" from
    # "abandoned". Written from the entitlement slice onward.
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    process_error: Mapped[str | None] = mapped_column(String, nullable=True)
    # Who settled this by hand, and as what. Null for everything the automatic
    # path decided — a hand-granted entitlement should be as traceable as one a
    # webhook produced.
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)
    # Identity of one delivery. UNIQUE is what makes a retry a no-op.
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)


# Built-in GrapeRank presets. One row per template (DEFAULT, PERMISSIVE, RESTRICTIVE).
# Seeded by migration with factory defaults, edited via admin endpoint.
class GrapeRankPreset(TimestampMixin, Base):
    __tablename__ = "graperank_preset"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    rigor: Mapped[float] = mapped_column(Float, nullable=False)
    attenuation_factor: Mapped[float] = mapped_column(Float, nullable=False)
    follow_rating: Mapped[float] = mapped_column(Float, nullable=False)
    follow_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    mute_rating: Mapped[float] = mapped_column(Float, nullable=False)
    mute_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    report_rating: Mapped[float] = mapped_column(Float, nullable=False)
    report_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    follow_confidence_of_observer: Mapped[float] = mapped_column(Float, nullable=False)
    verified_followers_influence_cutoff: Mapped[float] = mapped_column(Float, nullable=False)
    verified_reporters_influence_cutoff: Mapped[float] = mapped_column(Float, nullable=False)
    verified_muters_influence_cutoff: Mapped[float] = mapped_column(Float, nullable=False)


class ObserverWhitelist(TimestampMixin, Base):
    __tablename__ = "observerwhitelist"
    observer_pubkey: Mapped[str] = mapped_column(String, primary_key=True)
    # {observee_pubkey: influence} for above-cutoff observees only. Rounded
    # influence. Overwritten each successful run (1:1 per observer).
    scores: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_request_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("brainstorm_request.private_id"), nullable=True
    )


class GrapeRankPresetHistory(Base):
    __tablename__ = "graperank_preset_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    preset_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    rigor: Mapped[float] = mapped_column(Float, nullable=False)
    attenuation_factor: Mapped[float] = mapped_column(Float, nullable=False)
    follow_rating: Mapped[float] = mapped_column(Float, nullable=False)
    follow_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    mute_rating: Mapped[float] = mapped_column(Float, nullable=False)
    mute_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    report_rating: Mapped[float] = mapped_column(Float, nullable=False)
    report_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    follow_confidence_of_observer: Mapped[float] = mapped_column(Float, nullable=False)
    verified_followers_influence_cutoff: Mapped[float] = mapped_column(Float, nullable=False)
    verified_reporters_influence_cutoff: Mapped[float] = mapped_column(Float, nullable=False)
    verified_muters_influence_cutoff: Mapped[float] = mapped_column(Float, nullable=False)
    change_type: Mapped[str] = mapped_column(String, nullable=False)
    changed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    changed_at: Mapped[DateTime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
