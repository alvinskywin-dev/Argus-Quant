"""
Sprint 20C — request/response models for the exchange vault API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ConnectIn(BaseModel):
    exchange: str = Field(pattern="^(binance|okx|bybit|bitget)$")
    api_key: str = Field(min_length=4, max_length=256)
    api_secret: str = Field(min_length=4, max_length=256)
    passphrase: Optional[str] = Field(default=None, max_length=256)
    label: str = Field(default="default", min_length=1, max_length=64)


class AccountRef(BaseModel):
    exchange: str = Field(pattern="^(binance|okx|bybit|bitget)$")
    label: str = Field(default="default", min_length=1, max_length=64)


class ExchangeAccountOut(BaseModel):
    """Safe account view — never includes decrypted secrets."""
    id: int
    exchange: str
    label: str
    status: str
    api_key_last4: Optional[str]
    can_read: bool = False
    can_trade: bool
    can_futures: bool
    can_withdraw: bool
    last_validation_status: Optional[str] = None
    permission_warning: Optional[str] = None
    last_error: Optional[str]
    last_test: Optional[datetime]
    created_at: Optional[datetime]


class TestResultOut(BaseModel):
    exchange: str
    label: str
    status: str
    last_validation_status: Optional[str] = None
    can_read: bool = False
    can_trade: bool
    can_futures: bool
    can_withdraw: Optional[bool] = None
    permission_warning: Optional[str] = None
    error_code: Optional[str] = None
    message: str = ""


class MessageOut(BaseModel):
    detail: str
