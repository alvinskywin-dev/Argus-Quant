"""
Sprint 21E — net-PnL accounting persistence.

Four append-only tables. Raw exchange data is never overwritten; each row is an
immutable accounting record. MOCK and LIVE are kept separate via the `mode`
column so paper results never contaminate real performance.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base


class LiveTradeAccounting(Base):
    """Per-trade net-PnL breakdown."""

    __tablename__ = "live_trade_accounting"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    mode: Mapped[str] = mapped_column(String(8), default="MOCK")

    gross_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    funding_fee: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    net_roe: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    total_fees: Mapped[float] = mapped_column(Float, default=0.0)
    estimate_quality: Mapped[str] = mapped_column(String(12), default="PARTIAL")

    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    holding_time_seconds: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class DailyUserPnl(Base):
    """Per-user, per-day, per-mode aggregate."""

    __tablename__ = "daily_user_pnl"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    mode: Mapped[str] = mapped_column(String(8), default="MOCK")
    gross_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    funding_fee: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("user_id", "day", "mode", name="uq_daily_user_pnl"),)


class ExchangeFeeEvent(Base):
    """Commission / fee event (raw, never overwritten)."""

    __tablename__ = "exchange_fee_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32), default="")
    trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fee_type: Mapped[str] = mapped_column(String(16), default="COMMISSION")  # COMMISSION/SLIPPAGE
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    asset: Mapped[str] = mapped_column(String(12), default="USDT")
    mode: Mapped[str] = mapped_column(String(8), default="MOCK")
    estimated: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class FundingFeeEvent(Base):
    """Funding payment event (raw, never overwritten)."""

    __tablename__ = "funding_fee_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32), default="")
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # +received / -paid
    asset: Mapped[str] = mapped_column(String(12), default="USDT")
    mode: Mapped[str] = mapped_column(String(8), default="MOCK")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
