"""Database models: calls, call events and transcript turns."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# BIGINT identity on Postgres, plain INTEGER on SQLite (used in tests only).
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")
JsonType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(20), default="twilio")
    # Carrier call id (Twilio CallSid). Unique, so a call is stored once.
    provider_call_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    direction: Mapped[str | None] = mapped_column(String(20))
    from_number: Mapped[str | None] = mapped_column(String(32))
    to_number: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="ringing")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    # Filled when the call ends (see app/business/call_flush.py).
    outcome: Mapped[str | None] = mapped_column(String(20))  # booked | cancelled | info | silent
    turn_count: Mapped[int | None] = mapped_column(Integer)
    avg_response_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    events: Mapped[list["CallEvent"]] = relationship(back_populates="call")
    turns: Mapped[list["TranscriptTurn"]] = relationship(back_populates="call")


class CallEvent(Base):
    __tablename__ = "call_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    call: Mapped[Call] = relationship(back_populates="events")


class TranscriptTurn(Base):
    __tablename__ = "transcript_turns"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "caller" or "agent"
    text: Mapped[str] = mapped_column(Text)
    # Agent turns only: how long the caller waited, and where the time went (milliseconds).
    response_ms: Mapped[int | None] = mapped_column(Integer)
    stt_ms: Mapped[int | None] = mapped_column(Integer)
    llm_ms: Mapped[int | None] = mapped_column(Integer)
    tts_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    call: Mapped[Call] = relationship(back_populates="turns")


class Appointment(Base):
    """One booked consultation slot. The doctor sees one patient per slot."""

    __tablename__ = "appointments"
    __table_args__ = (
        # Two patients can never hold the same slot (cancelled rows do not count).
        Index(
            "uq_appointments_booked_slot",
            "slot_start",
            unique=True,
            postgresql_where=text("status = 'booked'"),
            sqlite_where=text("status = 'booked'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    patient_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str | None] = mapped_column(String(300))
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="booked")  # booked | cancelled
    booked_via_call: Mapped[str | None] = mapped_column(String(64))
    # "local" = our own booking, "calcom" = the booking lives in Cal.diy (external_id = its uid).
    source: Mapped[str] = mapped_column(String(20), default="local")
    external_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
