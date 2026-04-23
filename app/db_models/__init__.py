import enum
from sqlalchemy import DateTime, Integer, Float, String, func, Boolean, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils.auth.auth_util import generate_secure_password


class BrainstormRequestStatus(enum.Enum):
    WAITING = "waiting"
    ONGOING = "ongoing"
    SUCCESS = "success"
    FAILURE = "failure"


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
    result: Mapped[str] = mapped_column(String, nullable=True)
    count_values: Mapped[str] = mapped_column(String, nullable=True)
    parameters: Mapped[str] = mapped_column(String, nullable=False)
    algorithm: Mapped[str] = mapped_column(String, nullable=False)
    pubkey: Mapped[str] = mapped_column(String, nullable=True)
    graperank_preset_used: Mapped[str] = mapped_column(String, nullable=True)


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
    graperank_preset: Mapped[str] = mapped_column(String, nullable=True)
