"""
Sprint 20E — request/response models for the safety API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SafetyConfigOut(BaseModel):
    max_daily_loss_pct: float
    max_weekly_loss_pct: float
    max_open_positions: int
    max_correlated_positions: int
    max_leverage: int
    trade_cooldown_minutes: int
    loss_streak_limit: int
    loss_streak_cooldown_hours: int


class SafetyConfigUpdateIn(BaseModel):
    max_daily_loss_pct: Optional[float] = Field(default=None, ge=0, le=100)
    max_weekly_loss_pct: Optional[float] = Field(default=None, ge=0, le=100)
    max_open_positions: Optional[int] = Field(default=None, ge=1, le=100)
    max_correlated_positions: Optional[int] = Field(default=None, ge=1, le=100)
    max_leverage: Optional[int] = Field(default=None, ge=1, le=125)
    trade_cooldown_minutes: Optional[int] = Field(default=None, ge=0, le=1440)
    loss_streak_limit: Optional[int] = Field(default=None, ge=0, le=50)
    loss_streak_cooldown_hours: Optional[int] = Field(default=None, ge=0, le=720)


class SafetyStatusOut(BaseModel):
    trading_enabled: bool
    global_kill: bool
    kill_switch: bool
    disabled_until: Optional[datetime]
    disabled_reason: Optional[str]
    daily_pnl: float
    weekly_pnl: float
    loss_streak: int
    max_daily_loss_pct: float
    max_weekly_loss_pct: float


class MessageOut(BaseModel):
    detail: str
