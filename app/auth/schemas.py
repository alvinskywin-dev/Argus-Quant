"""
Sprint 20A — request/response models for the auth API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    username: Optional[str] = Field(default=None, min_length=3, max_length=64)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    totp_code: Optional[str] = Field(default=None, max_length=8)


class RefreshIn(BaseModel):
    refresh_token: str


class LogoutIn(BaseModel):
    refresh_token: str


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class VerifyEmailIn(BaseModel):
    token: str


class Enable2FAVerifyIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access-token lifetime, seconds


class TwoFactorRequired(BaseModel):
    two_factor_required: bool = True
    detail: str = "TOTP code required"


class UserOut(BaseModel):
    id: int
    email: EmailStr
    username: Optional[str]
    role: str
    status: str
    is_verified: bool
    totp_enabled: bool
    telegram_user_id: Optional[int]
    created_at: Optional[datetime]
    last_login_at: Optional[datetime]


class TwoFactorSetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class SessionOut(BaseModel):
    id: int
    ip: Optional[str]
    device: Optional[str]
    created_at: Optional[datetime]
    last_seen: Optional[datetime]
    expires_at: Optional[datetime]
    current: bool = False


class MessageOut(BaseModel):
    detail: str
