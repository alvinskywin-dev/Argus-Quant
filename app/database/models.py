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
    JSON,
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

    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)  # OPEN/TP1/TP2/TP3/SL/EXPIRED
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    max_favorable_pct: Mapped[float] = mapped_column(Float, default=0.0)
    max_adverse_pct: Mapped[float] = mapped_column(Float, default=0.0)

    telegram_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # MTF layer scores — nullable, only populated for V3.1+ signals
    trend_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    structure_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    setup_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Sprint 16A — full diagnostics object (JSON stored as text)
    diagnostics: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Sprint 16C — which RR method was selected ("atr" | "structure" | "liquidity")
    rr_method: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Sprint 19A — market regime at signal creation time
    market_regime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    regime_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

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

    # Sprint 16A / 16C — mirrors Signal
    diagnostics: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rr_method: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Sprint 19A — mirrors Signal
    market_regime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    regime_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class PaperPosition(Base):
    """
    Virtual paper-trading position created for each valid MTF signal.
    No real funds involved — purely simulated at 10 000 USDT starting balance,
    1% risk per trade.
    """
    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol:      Mapped[str]   = mapped_column(String(32), index=True)
    side:        Mapped[str]   = mapped_column(String(8))        # LONG / SHORT
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss:   Mapped[float] = mapped_column(Float)
    tp1:         Mapped[float] = mapped_column(Float)
    tp2:         Mapped[float] = mapped_column(Float, default=0.0)
    tp3:         Mapped[float] = mapped_column(Float, default=0.0)
    size_usdt:   Mapped[float] = mapped_column(Float, default=100.0)
    # OPEN | TP1 | TP2 | TP3 | SL | CLOSED
    status:      Mapped[str]   = mapped_column(String(16), default="OPEN")
    pnl_usdt:    Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct:     Mapped[float] = mapped_column(Float, default=0.0)
    opened_at:   Mapped[datetime]          = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    closed_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class FundingRateSnapshot(Base):
    __tablename__ = "funding_rate_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    funding_rate: Mapped[float] = mapped_column(Float)
    funding_time: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    next_funding_time: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    classification: Mapped[str] = mapped_column(String(32), default="neutral")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_funding_snapshots_symbol_created", "symbol", "created_at"),
    )


class OpenInterestSnapshot(Base):
    __tablename__ = "open_interest_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    open_interest: Mapped[float] = mapped_column(Float)
    oi_change_5m: Mapped[float] = mapped_column(Float, default=0.0)
    oi_change_15m: Mapped[float] = mapped_column(Float, default=0.0)
    oi_change_1h: Mapped[float] = mapped_column(Float, default=0.0)
    price_change_pct: Mapped[float] = mapped_column(Float, default=0.0)
    oi_score: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_oi_snapshots_symbol_created", "symbol", "created_at"),
    )


# ════════════════════════════════════════════════════════════════════
#  Sprint 20A — Multi-user SaaS auth
#
#  NOTE: these tables are intentionally separate from the legacy
#  telegram-keyed `users`/`User` table above. The SaaS account is keyed
#  by an auto-increment id with an email/password identity, and can be
#  optionally bridged to a telegram subscriber via `telegram_user_id`.
# ════════════════════════════════════════════════════════════════════


class AuthUser(Base):
    """A multi-user SaaS account (email + password identity)."""
    __tablename__ = "auth_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))

    role: Mapped[str] = mapped_column(String(16), default="FREE")       # ADMIN / PREMIUM / FREE
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")   # ACTIVE / SUSPENDED / PENDING
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # 2FA (TOTP)
    totp_secret: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Optional bridge to the existing telegram subscriber identity
    telegram_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)

    # Account lockout / login tracking
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuthSession(Base):
    """A refresh-token-backed login session for one device."""
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 hex
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    device: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)  # user-agent
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthToken(Base):
    """One-time token for email verification or password reset."""
    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))     # VERIFY / RESET
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 hex
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginHistory(Base):
    """Immutable record of every login attempt (success or failure)."""
    __tablename__ = "login_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auth_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(255), index=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    device: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# ════════════════════════════════════════════════════════════════════
#  Sprint 20B — Per-user paper (demo) futures accounts
#
#  Separate from the legacy GLOBAL paper engine (PaperPosition above,
#  table `paper_positions`), which simulates one portfolio for the whole
#  bot and feeds the public dashboard. These tables are per-AuthUser.
# ════════════════════════════════════════════════════════════════════


class PaperAccount(Base):
    """One virtual futures account per SaaS user (default 10,000 USDT)."""
    __tablename__ = "paper_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), unique=True, index=True
    )
    initial_balance: Mapped[float] = mapped_column(Float, default=10_000.0)
    balance: Mapped[float] = mapped_column(Float, default=10_000.0)   # realized wallet balance
    currency: Mapped[str] = mapped_column(String(8), default="USDT")
    default_leverage: Mapped[int] = mapped_column(Integer, default=10)
    auto_follow: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PaperAccountPosition(Base):
    """An open or closed simulated futures position inside a paper account."""
    __tablename__ = "paper_account_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), index=True
    )
    signal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))               # LONG / SHORT
    entry_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)             # base-asset units
    notional_usdt: Mapped[float] = mapped_column(Float)        # entry position value
    leverage: Mapped[int] = mapped_column(Integer, default=10)
    margin_usdt: Mapped[float] = mapped_column(Float)          # locked isolated margin
    liquidation_price: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tp1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tp2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tp3: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN/CLOSED/LIQUIDATED
    realized_pnl_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    funding_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    # Sprint 20D — managed by the auto-trading engine + protection state.
    auto_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    protection: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # BREAK_EVEN/TRAILING
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class PaperOrder(Base):
    """A simulated order. Market orders fill immediately; limit orders rest as NEW."""
    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), index=True
    )
    position_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("paper_account_positions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))              # LONG / SHORT
    order_type: Mapped[str] = mapped_column(String(8), default="MARKET")  # MARKET / LIMIT
    price: Mapped[float] = mapped_column(Float, default=0.0)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    notional_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(12), default="NEW")  # NEW/FILLED/CANCELLED/REJECTED
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class PaperTrade(Base):
    """Realized close record — the per-account trade history / PnL ledger."""
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), index=True
    )
    position_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    notional_usdt: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer, default=10)
    pnl_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)     # ROE = pnl / margin
    funding_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(16), default="MANUAL")  # TP1/2/3/SL/MANUAL/LIQUIDATION
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# ════════════════════════════════════════════════════════════════════
#  Sprint 20C — Exchange API credential vault
#
#  Secrets are stored ONLY as AES-256-GCM ciphertext. Withdrawal-enabled
#  keys are rejected at connect time and never persisted.
# ════════════════════════════════════════════════════════════════════


class ExchangeAccount(Base):
    __tablename__ = "exchange_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), index=True
    )
    exchange: Mapped[str] = mapped_column(String(16))   # binance / okx / bybit / bitget
    label: Mapped[str] = mapped_column(String(64), default="default")

    # Encrypted credentials (base64 nonce+ciphertext). Never plaintext.
    encrypted_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    encrypted_api_secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    encrypted_passphrase: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Non-sensitive display hint (last 4 chars of the API key).
    api_key_last4: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # CONNECTED/DISCONNECTED/ERROR
    can_read: Mapped[bool] = mapped_column(Boolean, default=False)
    can_trade: Mapped[bool] = mapped_column(Boolean, default=False)
    can_futures: Mapped[bool] = mapped_column(Boolean, default=False)
    can_withdraw: Mapped[bool] = mapped_column(Boolean, default=False)
    # Sprint 21A — real-permission-validation outcome (see permission_validator).
    last_validation_status: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    permission_warning: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    last_test: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "exchange", "label", name="uq_exchange_account"),
    )


class ExchangeAuditLog(Base):
    """Audit trail for vault actions (connect/test/disconnect)."""
    __tablename__ = "exchange_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auth_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    exchange: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(24))    # CONNECT/TEST/DISCONNECT/REJECT
    result: Mapped[str] = mapped_column(String(16))    # OK / FAIL / REJECTED
    detail: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# ════════════════════════════════════════════════════════════════════
#  Sprint 20D — Auto Trading Engine (DEMO MODE ONLY — paper accounts)
# ════════════════════════════════════════════════════════════════════


class AutoTradeConfig(Base):
    """Per-user auto-trading settings for the demo engine."""
    __tablename__ = "auto_trade_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), unique=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)        # Auto Trade ON/OFF
    max_positions: Mapped[int] = mapped_column(Integer, default=5)
    max_leverage: Mapped[int] = mapped_column(Integer, default=10)        # Allowed Leverage
    risk_per_trade_pct: Mapped[float] = mapped_column(Float, default=1.0)
    allowed_exchanges: Mapped[str] = mapped_column(String(128), default="")  # csv, "" = any
    allowed_coins: Mapped[str] = mapped_column(String(512), default="")      # csv base coins, "" = any
    min_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    order_type: Mapped[str] = mapped_column(String(8), default="MARKET")  # MARKET / LIMIT
    # Protection
    use_break_even: Mapped[bool] = mapped_column(Boolean, default=True)
    break_even_trigger: Mapped[str] = mapped_column(String(8), default="TP1")  # TP1/TP2
    use_trailing_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    trailing_distance_pct: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AutoTradeExecution(Base):
    """Audit/statistics record for every auto-engine decision and action."""
    __tablename__ = "auto_trade_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), index=True
    )
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    position_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(16))   # OPEN/SKIP/CLOSE/BREAK_EVEN/TRAIL
    reason: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# ════════════════════════════════════════════════════════════════════
#  Sprint 20E — Real Trading Safety Layer (account-destruction guards)
# ════════════════════════════════════════════════════════════════════


class SafetyConfig(Base):
    """Per-user protective limits applied before any auto open."""
    __tablename__ = "safety_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), unique=True, index=True
    )
    max_daily_loss_pct: Mapped[float] = mapped_column(Float, default=5.0)
    max_weekly_loss_pct: Mapped[float] = mapped_column(Float, default=15.0)
    max_open_positions: Mapped[int] = mapped_column(Integer, default=5)
    max_correlated_positions: Mapped[int] = mapped_column(Integer, default=3)
    max_leverage: Mapped[int] = mapped_column(Integer, default=20)
    trade_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=0)
    loss_streak_limit: Mapped[int] = mapped_column(Integer, default=3)
    loss_streak_cooldown_hours: Mapped[int] = mapped_column(Integer, default=24)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SafetyState(Base):
    """Mutable per-user trading-enabled state (kill switch + timed lockouts)."""
    __tablename__ = "safety_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), unique=True, index=True
    )
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)  # user emergency stop
    disabled_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_reason: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ════════════════════════════════════════════════════════════════════
#  Sprint 20F — Live trading (Binance USDT-M Futures)
#
#  These rows record real OR simulated execution; the `mode` column is
#  "LIVE" only when the live-trading gate was open at execution time,
#  otherwise "MOCK". No real order is placed unless mode == "LIVE".
# ════════════════════════════════════════════════════════════════════


class LiveOrder(Base):
    __tablename__ = "live_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), index=True
    )
    exchange: Mapped[str] = mapped_column(String(16))
    exchange_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))            # BUY / SELL
    order_type: Mapped[str] = mapped_column(String(24))     # MARKET/LIMIT/STOP_MARKET/...
    price: Mapped[float] = mapped_column(Float, default=0.0)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="NEW")
    mode: Mapped[str] = mapped_column(String(8), default="MOCK")   # MOCK / LIVE
    error: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class LivePosition(Base):
    __tablename__ = "live_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), index=True
    )
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))            # LONG / SHORT
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    margin_type: Mapped[str] = mapped_column(String(10), default="isolated")
    # OPEN / CLOSED / RECOVERED / CLOSED_UNKNOWN / UNSAFE
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    mode: Mapped[str] = mapped_column(String(8), default="MOCK")
    # Sprint 21B/21C — reconciliation + recovery safety state.
    tp_sl_status: Mapped[str] = mapped_column(String(16), default="UNKNOWN")  # see recovery.tp_sl
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False)
    unsafe_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    recovered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class LiveTrade(Base):
    __tablename__ = "live_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), index=True
    )
    position_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    pnl_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    mode: Mapped[str] = mapped_column(String(8), default="MOCK")
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class LiveAuditLog(Base):
    """Audit trail: every order, fill, error, and rejection."""
    __tablename__ = "live_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("auth_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(24))   # OPEN/CLOSE/LEVERAGE/TP_SL/ERROR/REJECT
    result: Mapped[str] = mapped_column(String(16))   # OK / FAIL / REJECTED
    mode: Mapped[str] = mapped_column(String(8), default="MOCK")
    detail: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


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

