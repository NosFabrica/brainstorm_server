import enum

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

    created_at = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at = mapped_column(
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

    __table_args__ = (
        # At most one default policy: partial unique index over the truthy rows.
        Index(
            "uq_scheduling_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )


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


# --------------------------------------------------------------------------
# Taggings (kind-39999) — the input set for Trusted Lists.
# See engineering-team/decisions/trusted-lists/0001-trusted-lists-from-taggings.md
# --------------------------------------------------------------------------


class NostrTagElement(Base):
    """A kind-39999 *tag element*: the declaration of a category ("Podcaster").

    Addressable at ``39999:<author_pubkey>:<slug>`` — tags with the same slug by
    different authors are DISTINCT elements, so the natural key is the pair, not
    the slug. ``event_id`` is carried because taggings reference the element by
    event id (`e` tag), which is how the deployed publishers write them.
    """

    __tablename__ = "nostr_tag_element"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    author_pubkey: Mapped[str] = mapped_column(String(64), nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    description: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    # Event's own created_at (unix seconds), NOT ingest time — replaceability is
    # decided on the event clock so out-of-order delivery can't regress a row.
    created_at_unix: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        # The addressable coordinate. A newer event at the same coordinate
        # replaces the row (latest-wins on created_at_unix).
        UniqueConstraint(
            "author_pubkey", "slug", name="uq_nostr_tag_element_coordinate"
        ),
    )


class NostrUserTagging(Base):
    """A kind-39999 *tagging*: an assertion that ``target_pubkey`` holds a tag.

    Replaceable on (asserter, d-tag): the deterministic ``d`` gives each asserter
    exactly one live stance per (target, tag), including flips between apply and
    dispute. Enforced at write time so reads are plain aggregates.
    """

    __tablename__ = "nostr_user_tagging"
    asserter_pubkey: Mapped[str] = mapped_column(String(64), primary_key=True)
    d_tag: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_pubkey: Mapped[str] = mapped_column(String(64), nullable=False)
    # The referenced tag element's event id (the `e` tag). Not an FK: taggings
    # legitimately arrive before the element they reference, and a dangling
    # reference is dropped at read time rather than rejected at write time.
    tag_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    polarity: Mapped[float] = mapped_column(Float, nullable=False, server_default="1")
    created_at_unix: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        Index("ix_nostr_user_tagging_tag_event_id", "tag_event_id"),
        Index("ix_nostr_user_tagging_target", "target_pubkey"),
    )
