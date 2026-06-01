"""
Sprint 21D — order failure / retry persistence.

One row per failed order attempt. The row tracks the classified error, the
retry budget consumed, when the next retry is due, and the terminal state.
Recording a failure is side-effect free w.r.t. the exchange — it never re-sends
an order by itself; the caller decides based on the RetryPolicy decision.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base


class OrderFailure(Base):
    __tablename__ = "order_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Idempotency key: stable per (signal/user/exchange/symbol/side) so the same
    # logical entry is never duplicated across retries.
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32), default="")
    order_type: Mapped[str] = mapped_column(String(24), default="MARKET")
    side: Mapped[str] = mapped_column(String(8), default="")
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(8), default="MOCK")

    error_class: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    error_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # PENDING / RETRY_SCHEDULED / NEEDS_RECONCILE / RESOLVED / FAILED
    final_state: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
