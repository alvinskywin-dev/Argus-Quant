"""
Sprint 20D — request/response models for the auto-trading API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AutoConfigOut(BaseModel):
    enabled: bool
    max_positions: int
    max_leverage: int
    risk_per_trade_pct: float
    allowed_exchanges: str
    allowed_coins: str
    min_confidence: float
    order_type: str
    use_break_even: bool
    break_even_trigger: str
    use_trailing_stop: bool
    trailing_distance_pct: float


class AutoConfigUpdateIn(BaseModel):
    enabled: Optional[bool] = None
    max_positions: Optional[int] = Field(default=None, ge=1, le=50)
    max_leverage: Optional[int] = Field(default=None, ge=1, le=125)
    risk_per_trade_pct: Optional[float] = Field(default=None, gt=0, le=100)
    allowed_exchanges: Optional[str] = Field(default=None, max_length=128)
    allowed_coins: Optional[str] = Field(default=None, max_length=512)
    min_confidence: Optional[float] = Field(default=None, ge=0, le=100)
    order_type: Optional[str] = Field(default=None, pattern="^(MARKET|LIMIT)$")
    use_break_even: Optional[bool] = None
    break_even_trigger: Optional[str] = Field(default=None, pattern="^(TP1|TP2)$")
    use_trailing_stop: Optional[bool] = None
    trailing_distance_pct: Optional[float] = Field(default=None, gt=0, le=50)


class ExecutionOut(BaseModel):
    id: int
    signal_id: Optional[int]
    account_id: Optional[int]
    position_id: Optional[int]
    symbol: str
    action: str
    reason: str
    detail: Optional[str]
    created_at: Optional[datetime]


class StatusOut(BaseModel):
    enabled: bool
    global_demo_enabled: bool
    open_auto_positions: int
    total_opened: int
    total_closed: int
    total_skipped: int


class MessageOut(BaseModel):
    detail: str
