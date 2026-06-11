"""Multi-user Live Beta — request/response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BetaRequestIn(BaseModel):
    invite_code: str = Field(default="", max_length=64)
    accept_risk: bool = False


class BetaAdminActionIn(BaseModel):
    user_id: int
    reason: str = Field(default="", max_length=256)


class MessageOut(BaseModel):
    message: str
