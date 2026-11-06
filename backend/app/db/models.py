"""ORM models mirroring migrations/0001_initial_schema.sql (the SQL file is the source of truth)."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _enum(name: str, *values: str) -> Enum:
    return Enum(*values, name=name, create_type=False)


Channel = _enum("channel_t", "whatsapp", "web")
JobStatus = _enum("job_status_t", "queued", "validating", "analyzing", "preparing", "generating",
                  "verifying", "completed", "failed", "cancelled", "review_required", "needs_more_images")
QaStatus = _enum("qa_status_t", "pending", "approved", "retry", "review_required", "rejected")
DeliveryStatus = _enum("delivery_status_t", "pending", "sent", "delivered", "failed")
ConversationState = _enum("conversation_state_t", "new", "awaiting_image", "awaiting_style", "confirmed",
                          "processing", "completed", "needs_more_images", "review_required", "failed")

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "review_required", "needs_more_images"}


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[uuid.UUID] = _uuid_pk()
    channel: Mapped[str] = mapped_column(Channel)
    external_ref_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    consent_version: Mapped[str | None] = mapped_column(Text)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_days: Mapped[int] = mapped_column(Integer, default=30)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(ConversationState, default="new")
    selected_style: Mapped[str | None] = mapped_column(Text)
    pending_image_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    updated_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InboundMessage(Base):
    __tablename__ = "inbound_messages"
    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    provider_message_id: Mapped[str] = mapped_column(Text, unique=True)
    media_id: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = _now()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Vehicle(Base):
    __tablename__ = "vehicles"
    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    profile: Mapped[dict | None] = mapped_column(JSONB)
    profile_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    storage_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(Integer)
    quality: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class GenerationJob(Base):
    __tablename__ = "generation_jobs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vehicles.id", ondelete="SET NULL"))
    source_upload_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(Channel)
    style: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text, default="same_angle")
    status: Mapped[str] = mapped_column(JobStatus, default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"))
    from_status: Mapped[str | None] = mapped_column(JobStatus)
    to_status: Mapped[str] = mapped_column(JobStatus)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class GeneratedImage(Base):
    __tablename__ = "generated_images"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"))
    attempt: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str | None] = mapped_column(Text)
    angle: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    qa_status: Mapped[str] = mapped_column(QaStatus, default="pending")
    qa: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class DeliveryEvent(Base):
    __tablename__ = "delivery_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(Channel)
    status: Mapped[str] = mapped_column(DeliveryStatus, default="pending")
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = _now()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class ReviewAction(Base):
    __tablename__ = "review_actions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"))
    reviewer_id: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(_enum("review_decision_t", "approve", "reject", "request_more_images"))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("generation_jobs.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    units: Mapped[float] = mapped_column(Numeric, default=1)
    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(10, 5), default=0)
    created_at: Mapped[datetime] = _now()
