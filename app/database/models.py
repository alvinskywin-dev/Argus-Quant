"""
SQLAlchemy ORM models — all persistent state lives here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))  # LONG / SHORT
    timeframe: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Float)
    risk_level: Mapped[str] = mapped_column(String(16))  # LOW/MEDIUM/HIGH
    strategy: Mapped[str] = mapped_column(String(64))
    reasons: Mapped[str] = mapped_column(Text, default="")

    entry_low: Mapped[float] = mapped_column(Float)
    entry_high: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float] = mapped_column(Float)
    tp2: Mapped[float] = mapped_column(Float)
    tp3: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    risk_reward: Mapped[float] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN/TP1/TP2/TP3/SL/EXPIRED
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    max_favorable_pct: Mapped[float] = mapped_column(Float, default=0.0)
    max_adverse_pct: Mapped[float] = mapped_column(Float, default=0.0)

    telegram_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # MTF layer scores — nullable, only populated for V3.1+ signals
    trend_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    structure_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    setup_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_signals_symbol_side_created", "symbol", "side", "created_at"),
    )


class Watchlist(Base):
    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_user_symbol"),)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram user id
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DailyStat(Base):
    __tablename__ = "daily_stats"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD
    signals_total: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    avg_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    best_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    worst_pnl: Mapped[float] = mapped_column(Float, default=0.0)


class WeeklyStat(Base):
    __tablename__ = "weekly_stats"

    week: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-WNN e.g. "2026-W22"
    signals_total: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    avg_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    best_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    worst_pnl: Mapped[float] = mapped_column(Float, default=0.0)


class AffiliateClick(Base):
    """Tracks affiliate link clicks for monetization reporting."""
    __tablename__ = "affiliate_clicks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)  # binance/bybit/okx/bitget
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    referrer: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)


class ArchivedSignal(Base):
    """
    Legacy signals moved out of production by archive_legacy_signals.py.

    Preserves every column from the signals table verbatim, plus two
    archive-specific columns: archive_reason and archived_at.
    """
    __tablename__ = "archive_signals"

    # ── archive metadata ──────────────────────────────────────────
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    original_id: Mapped[int] = mapped_column(Integer, index=True)
    archive_reason: Mapped[str] = mapped_column(String(64), default="")
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # ── original signals columns (mirrors Signal exactly) ─────────
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    timeframe: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Float)
    risk_level: Mapped[str] = mapped_column(String(16))
    strategy: Mapped[str] = mapped_column(String(64))
    reasons: Mapped[str] = mapped_column(Text, default="")

    entry_low: Mapped[float] = mapped_column(Float)
    entry_high: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float] = mapped_column(Float)
    tp2: Mapped[float] = mapped_column(Float)
    tp3: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    risk_reward: Mapped[float] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    max_favorable_pct: Mapped[float] = mapped_column(Float, default=0.0)
    max_adverse_pct: Mapped[float] = mapped_column(Float, default=0.0)

    telegram_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # MTF layer scores (nullable — only present on V3.1+ signals)
    trend_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    structure_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    setup_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class PaperPosition(Base):
    """Virtual paper-trading positions tracked against incoming signals."""
    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float] = mapped_column(Float)
    size_usdt: Mapped[float] = mapped_column(Float, default=100.0)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    pnl_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class SignalMessage(Base):
    __tablename__ = "signal_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"),
        index=True,
    )

    chat_id: Mapped[str] = mapped_column(String(64), index=True)

    telegram_message_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    signal = relationship("Signal")

