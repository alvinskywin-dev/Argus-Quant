"""
Auto Trading Foundation — Data Models.

IMPORTANT: Live trading is NOT enabled.
AUTO_TRADING_ENABLED is hard-locked to false in app/config.py.

This module defines the data architecture only:
  - Member: user account with encrypted API keys
  - RiskProfile: per-member risk settings
  - AutoTradingConfig: global auto-trading configuration
  - AuditLogEntry: immutable audit trail

No live orders are placed. No real money is touched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class RiskProfile:
    """Per-member risk management settings."""
    max_position_pct: float = 2.0       # Max % of balance per trade
    max_open_positions: int = 5         # Max simultaneous open positions
    daily_loss_limit_pct: float = 5.0   # Stop trading if daily loss > this %
    max_leverage: int = 10              # Maximum leverage allowed
    allowed_tiers: List[str] = field(default_factory=lambda: ["ELITE", "VIP"])
    min_confidence: float = 85.0        # Minimum signal confidence to trade
    min_rr: float = 2.5                 # Minimum risk/reward ratio


@dataclass
class Member:
    """
    Represents a member who has opted in to auto-trading.

    API keys are stored encrypted (AES-256-GCM).
    Plain-text keys are NEVER stored.
    """
    id: int
    telegram_user_id: int
    username: Optional[str]
    exchange: str                       # binance / bybit / okx / bitget
    api_key_encrypted: str              # AES-256-GCM encrypted
    api_secret_encrypted: str           # AES-256-GCM encrypted
    risk_profile: RiskProfile = field(default_factory=RiskProfile)
    is_active: bool = False             # Requires explicit opt-in
    emergency_stop: bool = False        # If True, no new trades
    created_at: str = ""
    last_trade_at: Optional[str] = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


@dataclass
class AutoTradingConfig:
    """
    Global auto-trading system configuration.

    Can only be activated when AUTO_TRADING_ENABLED=true AND
    emergency_stop is False.
    """
    enabled: bool = False               # Always False (locked in config.py)
    emergency_stop: bool = False        # Global kill switch
    max_members: int = 100
    supported_exchanges: List[str] = field(default_factory=lambda: [
        "binance", "bybit", "okx", "bitget"
    ])
    allowed_signal_tiers: List[str] = field(default_factory=lambda: ["ELITE", "VIP"])


@dataclass
class AuditLogEntry:
    """
    Immutable audit trail entry.

    Every auto-trading action generates an audit log entry.
    Entries can never be deleted or modified.
    """
    id: Optional[int]
    member_id: int
    action: str                         # OPEN / CLOSE / CANCEL / EMERGENCY_STOP / SETTINGS_CHANGE
    symbol: Optional[str]
    side: Optional[str]
    size_usdt: Optional[float]
    reason: str
    signal_id: Optional[int]
    result: Optional[str]               # SUCCESS / FAILED / REJECTED
    error: Optional[str]
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    @property
    def is_trade_action(self) -> bool:
        return self.action in ("OPEN", "CLOSE", "CANCEL")
