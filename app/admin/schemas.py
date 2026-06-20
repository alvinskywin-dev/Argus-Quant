"""
Sprint 20H — Admin Dashboard request schemas.

Response bodies are plain dicts assembled in the service (heterogeneous rollups),
so only request payloads are modelled here.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SetUserStatusIn(BaseModel):
    status: str = Field(pattern="^(ACTIVE|SUSPENDED)$")


class LiveTradingToggleIn(BaseModel):
    enabled: bool
    # Required to ENABLE real trading (ignored when disabling). Guards against
    # an accidental click flipping the bot to real money.
    confirm: str = ""
