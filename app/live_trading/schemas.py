"""
Sprint 20F — request/response models for the live-trading API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class OpenLiveIn(BaseModel):
    # "auto" routes to the user's connected exchange (Signal -> Exchange Adapter).
    exchange: str = Field(default="auto", pattern="^(auto|binance|okx|bybit|bitget)$")
    symbol: str = Field(min_length=2, max_length=32)
    side: str = Field(pattern="^(LONG|SHORT)$")
    quantity: Optional[float] = Field(default=None, gt=0)
    notional_usdt: Optional[float] = Field(default=None, gt=0)
    entry_price: Optional[float] = Field(default=None, gt=0)
    leverage: int = Field(default=5, ge=1, le=125)
    margin_type: str = Field(default="isolated", pattern="^(isolated|cross)$")
    order_type: str = Field(default="MARKET", pattern="^(MARKET|LIMIT)$")
    take_profit: Optional[float] = Field(default=None, gt=0)
    stop_loss: Optional[float] = Field(default=None, gt=0)
    trailing_pct: Optional[float] = Field(default=None, gt=0, le=10)


class CloseLiveIn(BaseModel):
    position_id: int
    exit_price: Optional[float] = Field(default=None, gt=0)


class SetLeverageIn(BaseModel):
    exchange: str = Field(pattern="^(binance|okx|bybit|bitget)$")
    symbol: str = Field(min_length=2, max_length=32)
    leverage: int = Field(ge=1, le=125)


class GateStatusOut(BaseModel):
    live_trading_enabled: bool
    mock_exchange_mode: bool
    live_gate_open: bool
    mode: str


class LivePositionOut(BaseModel):
    id: int
    exchange: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    leverage: int
    margin_type: str
    status: str
    realized_pnl: float
    mode: str
    opened_at: Optional[datetime]
    closed_at: Optional[datetime]


class LiveOrderOut(BaseModel):
    id: int
    exchange: str
    exchange_order_id: Optional[str]
    symbol: str
    side: str
    order_type: str
    price: float
    quantity: float
    filled_qty: float
    reduce_only: bool
    status: str
    mode: str
    error: Optional[str]
    created_at: Optional[datetime]


class LiveTradeOut(BaseModel):
    id: int
    exchange: str
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    leverage: int
    pnl_usdt: float
    mode: str
    opened_at: Optional[datetime]
    closed_at: Optional[datetime]
