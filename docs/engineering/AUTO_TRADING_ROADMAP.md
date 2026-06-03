# AUTO-TRADING ROADMAP — ALPHA RADAR SIGNALS

> Status: **NOT IMPLEMENTED** — Architecture design only.
> Public dashboard launch complete. Auto-trading is a future phase.

---

## Phase 1 — Member Account System

### Goals
- Allow members to register and link their own exchange API keys
- Each member controls their own risk settings independently
- No pooled funds — every trade executes on the member's own account

### Member Data Model
```
members
  id              UUID PK
  email           VARCHAR unique
  password_hash   VARCHAR (bcrypt)
  created_at      TIMESTAMP
  is_active       BOOL
  plan            ENUM(free, basic, pro)

member_api_keys
  id              UUID PK
  member_id       UUID FK → members.id
  exchange        ENUM(binance, bybit, okx, bitget)
  api_key_enc     TEXT  (AES-256-GCM encrypted at rest)
  api_secret_enc  TEXT
  label           VARCHAR
  created_at      TIMESTAMP
  last_used_at    TIMESTAMP
```

### API Key Security Requirements
- Keys encrypted with AES-256-GCM before storage (per-member salt)
- Master encryption key stored in environment variable, never in DB
- Keys decrypted only at order-execution time, never logged
- Only **futures trading** permission allowed — no spot, no withdrawals
- Read-only health check on key submission to validate permissions
- Keys can be deleted (zeroed) at any time by member

---

## Phase 2 — Risk Settings Per Member

```
member_risk_settings
  member_id           UUID PK FK
  copy_signals        BOOL    default FALSE  ← must opt-in explicitly
  max_leverage        INT     default 5       (1–20)
  max_margin_pct      FLOAT   default 1.0     (% of account per trade)
  max_open_trades     INT     default 3
  mandatory_sl        BOOL    default TRUE    (cannot be disabled)
  emergency_stop      BOOL    default FALSE   (kills all open trades)
  min_signal_conf     FLOAT   default 80.0
  allowed_tiers       VARCHAR default 'PUBLIC'  (PUBLIC,VIP,ELITE)
```

### Copy Signal Flow (per signal event)
1. Signal emitted by scanner (existing pipeline)
2. Fan-out: for each opted-in member with `copy_signals=TRUE`
3. Validate signal meets member's `min_signal_conf` and `allowed_tiers`
4. Check `max_open_trades` — skip if limit reached
5. Calculate position size = `max_margin_pct × account_balance`
6. Apply `max_leverage` cap
7. Place order: entry at market or limit, TP1/TP2/TP3, mandatory SL
8. Record in `member_trades` table for audit
9. Update open trade counter

---

## Phase 3 — Order Execution Layer

### Exchange Adapter Interface
```python
class ExchangeAdapter(Protocol):
    async def place_futures_order(
        self,
        symbol: str,
        side: Literal["LONG", "SHORT"],
        entry: float,
        tp_levels: list[float],
        sl: float,
        leverage: int,
        margin_usdt: float,
    ) -> TradeResult: ...

    async def cancel_order(self, order_id: str) -> None: ...
    async def get_balance(self) -> float: ...
    async def get_open_positions(self) -> list[Position]: ...
```

### Supported Exchanges (planned)
| Exchange | SDK | Notes |
|----------|-----|-------|
| Binance  | python-binance / ccxt | Primary |
| Bybit    | pybit / ccxt | Secondary |
| OKX      | okx-sdk / ccxt | Tertiary |
| Bitget   | ccxt | Tertiary |

### Order Types
- Market entry (immediate fill) — default
- Limit entry (post-only, timeout 5 min then cancel)
- OCO: TP + SL placed immediately after entry fill

---

## Phase 4 — Audit Log & Emergency Controls

```
member_trades
  id              UUID PK
  member_id       UUID FK
  signal_id       INT FK → signals.id
  exchange        VARCHAR
  exchange_order_id VARCHAR
  side            VARCHAR
  symbol          VARCHAR
  entry_price     FLOAT
  sl_price        FLOAT
  quantity        FLOAT
  margin_usdt     FLOAT
  leverage        INT
  status          ENUM(pending, open, tp1, tp2, tp3, sl, cancelled, error)
  pnl_usdt        FLOAT
  opened_at       TIMESTAMP
  closed_at       TIMESTAMP

audit_log
  id              BIGSERIAL PK
  member_id       UUID nullable
  event           VARCHAR   (key_added, key_deleted, order_placed, emergency_stop, ...)
  detail          JSONB
  ip_address      VARCHAR
  created_at      TIMESTAMP
```

### Emergency Stop
- Per-member toggle in risk settings
- When activated: cancels all pending orders, closes open positions at market
- Logged in audit_log with timestamp and IP
- Requires re-confirmation to disable

---

## Phase 5 — Member Dashboard

- Portfolio overview (balance, open trades, PnL)
- Trade history per signal
- API key management (add/delete, never show plaintext after save)
- Risk settings editor
- Emergency stop button (prominent, red)
- Opt-in/opt-out copy trading toggle

---

## Security Checklist (before any live trading)

- [ ] Penetration test on API key storage and retrieval
- [ ] Rate limiting on all member endpoints
- [ ] 2FA required for API key management
- [ ] Withdrawal permission check at key registration (reject if present)
- [ ] IP allowlist support for exchange API keys
- [ ] Regular key rotation reminders
- [ ] All order errors logged and member notified
- [ ] Killswitch: admin-level emergency stop for all members
- [ ] Legal review: terms of service, liability disclaimer, jurisdiction compliance
- [ ] Regulatory assessment (depending on operating country)

---

## Implementation Sequence

```
Phase 1  →  Member accounts + encrypted key storage
Phase 2  →  Risk settings schema + validation
Phase 3  →  Exchange adapters (Binance first)
Phase 4  →  Audit logging + emergency stop
Phase 5  →  Member-facing dashboard
         →  Beta with 5–10 volunteer testers
         →  Security audit
         →  Public launch
```

---

> **IMPORTANT:** No live trading code exists in this codebase yet.
> All order execution must be tested on testnet before mainnet.
> Legal and regulatory review required before offering auto-trading
> to members in any jurisdiction.
