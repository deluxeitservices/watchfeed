from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # 1203...@g.us
    name: Mapped[str] = mapped_column(String(300), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    participants: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_id: Mapped[str] = mapped_column(String(200), unique=True)
    group_id: Mapped[str] = mapped_column(String(80), index=True)
    sender_jid: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    sender_phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    sender_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    # NEW -> SKIPPED | PARSED | NO_OFFERS | ERROR
    status: Mapped[str] = mapped_column(String(20), default="NEW", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    offers_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ParseCache(Base):
    """Dealers copy-paste the same list into many groups: parse once."""
    __tablename__ = "parse_cache"
    text_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    group_id: Mapped[str] = mapped_column(String(80), index=True)
    groups_seen: Mapped[str] = mapped_column(Text, default="")  # comma-separated group ids

    direction: Mapped[str] = mapped_column(String(3), index=True)  # WTS / WTB
    brand: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    family: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    reference: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    reference_norm: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    dial_color: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    case_material: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    bracelet: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    diamond_indices: Mapped[bool] = mapped_column(Boolean, default=False)
    condition: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    set_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    price_base: Mapped[Optional[float]] = mapped_column(Float, index=True, nullable=True)
    discount_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_text: Mapped[str] = mapped_column(Text, default="")

    dealer_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    dealer_phone: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)

    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


Index("ix_offers_dir_seen", Offer.direction, Offer.last_seen_at)

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
