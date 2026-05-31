"""
Sprint 20B — request/response models for the paper-trading API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class OpenPositionIn(BaseModel):
    symbol: str = Field(min_length=2, max_length=32)
    side: str = Field(pattern="^(LONG|SHORT)$")
    entry_price: float = Field(gt=0)
    leverage: int = Field(default=10, ge=1, le=125)
    margin_usdt: Optional[float] = Field(default=None, gt=0)
    notional_usdt: Optional[float] = Field(default=None, gt=0)
    stop_loss: Optional[float] = Field(default=None, ge=0)
    tp1: Optional[float] = Field(default=None, ge=0)
    tp2: Optional[float] = Field(default=None, ge=0)
    tp3: Optional[float] = Field(default=None, ge=0)
    order_type: str = Field(default="MARKET", pattern="^(MARKET|LIMIT)$")


class FromSignalIn(BaseModel):
    signal_id: int
    leverage: Optional[int] = Field(default=None, ge=1, le=125)


class ClosePositionIn(BaseModel):
    mark_price: Optional[float] = Field(default=None, gt=0)
    reason: str = Field(default="MANUAL", max_length=16)


class AutoFollowIn(BaseModel):
    enabled: bool


class AccountSummaryOut(BaseModel):
    account_id: int
    currency: str
    initial_balance: float
    balance: float
    used_margin: float
    available_balance: float
    unrealized_pnl: float
    equity: float
    open_positions: int
    realized_pnl: float
    total_pnl: float
    daily_pnl: float
    win_rate: float
    total_trades: int
    auto_follow: bool
    default_leverage: int


class PositionOut(BaseModel):
    id: int
    symbol: str
    side: str
    entry_price: float
    quantity: float
    notional_usdt: float
    leverage: int
    margin_usdt: float
    liquidation_price: float
    stop_loss: Optional[float]
    tp1: Optional[float]
    tp2: Optional[float]
    tp3: Optional[float]
    status: str
    mark_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    roe_pct: Optional[float] = None
    realized_pnl_usdt: float
    funding_usdt: float
    signal_id: Optional[int]
    opened_at: Optional[datetime]
    closed_at: Optional[datetime]


class OrderOut(BaseModel):
    id: int
    symbol: str
    side: str
    order_type: str
    price: float
    quantity: float
    notional_usdt: float
    reduce_only: bool
    status: str
    position_id: Optional[int]
    created_at: Optional[datetime]
    filled_at: Optional[datetime]


class TradeOut(BaseModel):
    id: int
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    notional_usdt: float
    leverage: int
    pnl_usdt: float
    pnl_pct: float
    funding_usdt: float
    reason: str
    signal_id: Optional[int]
    opened_at: Optional[datetime]
    closed_at: Optional[datetime]


class SimulationOut(BaseModel):
    symbol: str
    side: str
    entry_price: float
    leverage: int
    notional_usdt: float
    margin_usdt: float
    quantity: float
    liquidation_price: float
    projections: dict  # {"TP1": {price, pnl_usdt, roe_pct}, ...}


class MessageOut(BaseModel):
    detail: str
