"""
Telegram Community Consolidation — routing, message format, flags, dashboard link.

All signal flows merge into the single flagship public group during the
optimization/validation phase. These tests pin that behaviour without any
network or running bot.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings, settings
from app.telegram_bot import bot as tgbot
from app.telegram_bot.formatter import format_community_signal


def _sig(confidence=96.0, tier="ELITE"):
    """A high-confidence (would-be ELITE) signal dict."""
    return {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "confidence": confidence,
        "risk_reward": 2.1,
        "market_regime": "LOW_VOLATILITY",
        "reasons": "4H bullish structure intact | Liquidity sweep below support",
        "entry_low": 102400.0,
        "entry_high": 102500.0,
        "tp1": 104200.0,
        "tp2": 105900.0,
        "tp3": 107000.0,
        "stop_loss": 99880.0,
        "timeframe": "15m",
        "_tier": tier,
        "diagnostics": json.dumps(
            {"stoploss_engine_mode": "BALANCED", "market_regime": "LOW_VOLATILITY"}
        ),
    }


@pytest.fixture
def cfg(monkeypatch):
    """Pin the consolidation settings + env, then restore."""
    keys = [
        "telegram_community_mode",
        "telegram_single_public_group",
        "public_telegram_chat_id",
        "public_chat_id",
        "telegram_signal_chat_id",
        "vip_telegram_disabled",
        "elite_telegram_disabled",
        "premium_telegram_disabled",
        "vip_routing_enabled",
        "elite_routing_enabled",
        "premium_routing_enabled",
    ]
    saved = {k: getattr(settings, k) for k in keys}
    settings.telegram_community_mode = True
    settings.telegram_single_public_group = True
    settings.public_telegram_chat_id = "-1009999999"
    settings.public_chat_id = ""
    settings.telegram_signal_chat_id = "-100fallback"
    settings.vip_telegram_disabled = True
    settings.elite_telegram_disabled = True
    settings.premium_telegram_disabled = True
    settings.vip_routing_enabled = False
    settings.elite_routing_enabled = False
    settings.premium_routing_enabled = False
    # Tier-specific chats exist in the env but must be ignored in community mode.
    monkeypatch.setenv("ELITE_VIP_CHAT_ID", "-100elite")
    monkeypatch.setenv("VIP_CHAT_ID", "-100vip")
    monkeypatch.setenv("PUBLIC_CHAT_ID", "-100legacypublic")
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


# 1. All signals route to the single public group, regardless of tier.
def test_all_signals_route_to_public_group(cfg):
    assert tgbot._route_signal_chats(_sig(confidence=96, tier="ELITE")) == ["-1009999999"]
    assert tgbot._route_signal_chats(_sig(confidence=88, tier="VIP")) == ["-1009999999"]
    assert tgbot._route_signal_chats(_sig(confidence=78, tier="PUBLIC")) == ["-1009999999"]


# 2. No VIP / Elite sends when consolidation is active.
def test_no_vip_or_elite_sends_when_disabled(cfg):
    for tier in ("ELITE", "VIP", "PUBLIC"):
        chats = tgbot._route_signal_chats(_sig(tier=tier))
        assert "-100elite" not in chats
        assert "-100vip" not in chats
        assert chats == ["-1009999999"]


# 3. Backward compatibility — falls back to TELEGRAM_SIGNAL_CHAT_ID when neither
#    the new PUBLIC_TELEGRAM_CHAT_ID nor the legacy PUBLIC_CHAT_ID is set.
def test_backward_compatible_fallback(cfg, monkeypatch):
    settings.public_telegram_chat_id = ""
    monkeypatch.delenv("PUBLIC_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("PUBLIC_CHAT_ID", raising=False)
    chats = tgbot._route_signal_chats(_sig())
    assert chats == ["-100fallback"]


# 3b. Legacy PUBLIC_CHAT_ID still works as the public target (back-compat).
def test_legacy_public_chat_id_honored(cfg):
    settings.public_telegram_chat_id = ""
    # env PUBLIC_CHAT_ID is "-100legacypublic" from the fixture
    assert tgbot._route_signal_chats(_sig()) == ["-100legacypublic"]


# 4. Env flags respected — turning consolidation off restores tier routing,
#    and an enabled tier with its disable switch off reaches its own chat.
def test_flags_respected_legacy_routing(cfg):
    settings.telegram_community_mode = False
    settings.telegram_single_public_group = False

    # All tier routing still disabled → ELITE falls back to the default chat.
    assert tgbot._route_signal_chats(_sig(tier="ELITE")) == ["-100fallback"]

    # Re-enable elite routing → ELITE now reaches the elite chat.
    settings.elite_routing_enabled = True
    settings.elite_telegram_disabled = False
    assert tgbot._route_signal_chats(_sig(tier="ELITE")) == ["-100elite"]

    # Even with routing enabled, the disable switch wins.
    settings.elite_telegram_disabled = True
    assert tgbot._route_signal_chats(_sig(tier="ELITE")) == ["-100fallback"]


# 5. No broadcast target → empty list (never crashes).
def test_no_target_returns_empty(cfg, monkeypatch):
    settings.public_telegram_chat_id = ""
    settings.public_chat_id = ""
    settings.telegram_signal_chat_id = ""
    monkeypatch.delenv("PUBLIC_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("PUBLIC_CHAT_ID", raising=False)
    assert tgbot._route_signal_chats(_sig()) == []


# 6. Community message format carries the Phase-2 required fields.
def test_community_message_format(cfg):
    text = format_community_signal(_sig())
    assert "ARGUS QUANT SIGNAL" in text
    assert "Confidence: <b>96.0%</b>" in text
    assert "RR: <b>1 : 2.1</b>" in text
    assert "LOW_VOLATILITY" in text
    assert "BALANCED ATR+STRUCTURE" in text  # stoploss mode label
    assert "Why this trade?" in text
    assert "Risk:" in text  # risk warning present


# 7. Dashboard / community link default points at the flagship public community.
#    (Asserts the code default; the live value may be overridden via .env.)
def test_dashboard_community_link():
    assert Settings.model_fields["telegram_channel_url"].default == "https://t.me/ArgusQuant"
