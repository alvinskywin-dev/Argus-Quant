"""
Sprint 21B — reconciliation persistence model.

A ReconciliationIssue records a single detected drift between the local DB and
the exchange. Rows are append-only audit records: detection NEVER mutates
positions or places orders. Resolution is an explicit, separate safety action.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base


class ReconciliationIssue(Base):
    __tablename__ = "reconciliation_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32), default="")
    mode: Mapped[str] = mapped_column(String(8), default="MOCK")     # MOCK / LIVE
    severity: Mapped[str] = mapped_column(String(8), default="WARNING")  # INFO/WARNING/CRITICAL
    issue_type: Mapped[str] = mapped_column(String(40))
    db_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)        # JSON
    exchange_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON
    recommended_action: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
