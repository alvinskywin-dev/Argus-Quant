# Auto Trading Architecture — ALPHA RADAR SIGNALS

> **STATUS: ARCHITECTURE ONLY — Live trading is NOT enabled.**
> `AUTO_TRADING_ENABLED` is hard-locked to `false` in `app/config.py`.
> No real orders are placed. No real money is touched.

## Overview

This document describes the planned architecture for a future opt-in auto-trading feature. Members would be able to connect their own exchange API keys and have signals executed automatically according to their risk profile.

## Module Structure

```
app/auto_trading/
├── __init__.py          # Public exports
├── models.py            # Data models (Member, RiskProfile, AuditLog)
├── executor.py          # [PLANNED] Order execution engine
├── position_manager.py  # [PLANNED] Open position tracking
├── risk_manager.py      # [PLANNED] Real-time risk enforcement
└── key_vault.py         # [PLANNED] Encrypted key storage/retrieval
```

## Data Models

### Member
- `telegram_user_id` — links member to their Telegram identity
- `exchange` — one of: binance, bybit, okx, bitget
- `api_key_encrypted` — AES-256-GCM encrypted, never stored plain-text
- `risk_profile` — individual risk settings
- `emergency_stop` — per-member kill switch

### RiskProfile
| Setting | Default | Description |
|---------|---------|-------------|
| `max_position_pct` | 2% | Max % of balance per trade |
| `max_open_positions` | 5 | Maximum simultaneous positions |
| `daily_loss_limit_pct` | 5% | Auto-stop if daily loss exceeds this |
| `max_leverage` | 10x | Maximum allowed leverage |
| `min_confidence` | 85% | Only trade VIP+ signals |
| `min_rr` | 2.5 | Minimum risk/reward |

### AuditLog
Every auto-trading action is recorded in an immutable audit log:
- Action type (OPEN/CLOSE/CANCEL/EMERGENCY_STOP)
- Member ID, Signal ID, Symbol, Side, Size
- Result (SUCCESS/FAILED/REJECTED)
- Error message if applicable
- Timestamp

## Safety Architecture

```
Signal Generated
      │
      ▼
  Is AUTO_TRADING_ENABLED=true?  ──No──► Skip auto-trading
      │ Yes
      ▼
  Is global EMERGENCY_STOP=false?  ──No──► Cancel all, alert
      │ Yes
      ▼
  For each opted-in Member:
    Is member.emergency_stop=false?  ──No──► Skip member
      │ Yes
      ▼
    Does signal meet member's RiskProfile?
    (confidence, RR, tier, exchange)  ──No──► Log rejection
      │ Yes
      ▼
    Calculate position size (risk_pct × balance)
      │
      ▼
    Is daily loss limit NOT exceeded?  ──No──► Log rejection
      │ Yes
      ▼
    Place order via exchange API
      │
      ▼
    Record in AuditLog
      │
      ▼
    Notify member via Telegram
```

## API Key Security

1. Keys are collected via an encrypted Telegram flow — never sent in plain text via web.
2. Keys are encrypted with AES-256-GCM before storage.
3. Encryption key is derived from `SECRET_KEY` + member-specific salt.
4. Decryption only happens at order execution time.
5. Read-only verification of keys is performed on registration.
6. Members can revoke access (delete keys) at any time.
7. IP whitelisting on exchange API keys is strongly recommended.

## Emergency Stop

Two levels:
1. **Global**: Set `emergency_stop=true` in `AutoTradingConfig` → no new orders for any member.
2. **Per-member**: Member's `emergency_stop=true` → no new orders for that member only.

Emergency stop is triggered automatically if:
- Daily loss limit exceeded
- Exchange API error rate > threshold
- Unusual order rejection pattern detected

## Roadmap

- [ ] Phase 1: Member registration & API key encryption (Telegram flow)
- [ ] Phase 2: Paper trading validation (uses `app/paper_trading/`)
- [ ] Phase 3: Risk manager (real-time position + daily loss tracking)
- [ ] Phase 4: Order executor (exchange API integration)
- [ ] Phase 5: Admin dashboard — auto-trading panel
- [ ] Phase 6: Subscription tiers (limits auto-trading to premium members)
- [ ] Phase 7: Copy trading (one member mirrors another's trades)

## Compliance Notes

- Users must explicitly opt in and acknowledge risk disclosures.
- Each member is responsible for their own trading decisions.
- ALPHA RADAR SIGNALS is not a licensed broker or investment advisor.
- Jurisdictional restrictions may apply — users must verify legality in their country.
- All signal usage is governed by the Terms of Service and Risk Disclaimer.
