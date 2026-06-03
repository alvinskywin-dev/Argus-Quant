"""
Multi-user Live Beta — membership model.

One row per user enrolled in (or requesting) the live beta. Append-only audit
fields (requested/approved timestamps, risk-agreement acceptance). Per-user
risk limits default from settings but can be tightened per member by an admin.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base

# status values
PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
SUSPENDED = "SUSPENDED"


class LiveBetaMember(Base):
    __tablename__ = "live_beta_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default=PENDING, index=True)

    # Per-user risk envelope (USDT notional + position count). Seeded from
    # settings at request time; an admin may tighten them.
    max_notional: Mapped[float] = mapped_column(Float, default=100.0)
    max_positions: Mapped[int] = mapped_column(Integer, default=2)
    # Comma-separated exchange allowlist for this member.
    allowed_exchanges: Mapped[str] = mapped_column(String(128), default="binance")

    invite_code_used: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    risk_agreement_accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    approved_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
