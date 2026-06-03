"""
Central configuration. All other modules import `settings` from here.
Values are read from environment / .env via pydantic-settings.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Binance ---
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = False

    # ── 21F — Binance Futures testnet validation (separate keys; default OFF) ──
    binance_testnet_enabled: bool = False
    binance_testnet_base_url: str = "https://testnet.binancefuture.com"
    binance_testnet_ws_url: str = "wss://stream.binancefuture.com/ws"
    binance_testnet_api_key: str = ""
    binance_testnet_api_secret: str = ""

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_signal_chat_id: str = ""
    telegram_admin_ids: str = ""

    # --- Database ---
    postgres_user: str = "signals"
    postgres_password: str = "signals"
    postgres_db: str = "signals"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # --- Redis ---
    redis_host: str = "redis"
    redis_port: int = 6379

    # --- Scanner ---
    universe_refresh_sec: int = 900
    scan_interval_sec: int = 30
    scan_timeframes: str = "15m,1h,4h,1d"
    max_symbols: int = 0
    min_quote_volume_usdt: float = 5_000_000

    # --- Signal engine ---
    min_confidence: float = 75.0
    signal_cooldown_sec: int = 1800
    symbol_cooldown_minutes: int = 30
    anti_duplicate_signal: bool = True
    max_signals_per_hour: int = 12
    min_rr: float = 2.0
    entry_pass_score: int = 2  # minimum 15M entry factors needed (0-5)

    # --- Duplicate signal prevention ---
    # Block any new signal for a symbol while it still has an OPEN position
    block_same_symbol_while_open: bool = True
    # Also block if same symbol AND same side is open (applies when block_same_symbol_while_open=False)
    block_same_symbol_side_while_open: bool = True
    # Hours to suppress re-entry for the same symbol+side after a TP or SL close (0 = disabled)
    signal_duplicate_cooldown_hours: int = 24

    # --- Dashboard ---
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8010
    dashboard_secret: str = ""
    dashboard_user: str = "admin"
    dashboard_password: str = ""
    secret_key: str = ""

    # --- Community ---
    telegram_channel_url: str = ""
    discord_url: str = ""

    # --- Donations ---
    donate_usdt_trc20: str = ""
    donate_usdt_bep20: str = ""
    donate_btc: str = ""
    donate_eth: str = ""

    # --- Affiliates ---
    binance_affiliate_url: str = ""
    bybit_affiliate_url: str = ""
    okx_affiliate_url: str = ""
    bitget_affiliate_url: str = ""

    # --- Logging ---
    log_level: str = "INFO"
    log_dir: str = "/app/logs"
    log_rejection_detail: bool = False
    log_retention_days: int = 30
    log_max_size_mb: int = 100

    # --- Paper Trading ---
    paper_trading: bool = False
    paper_initial_balance: float = 10_000.0
    paper_risk_per_trade_pct: float = 1.0

    # --- Auto Trading Foundation (architecture only, never enabled) ---
    auto_trading_enabled: bool = False
    auto_trading_max_position_pct: float = 2.0
    auto_trading_daily_loss_limit_pct: float = 5.0

    # --- Live Trading master safety gate (Sprint 20D-20G) ---
    # Global kill switch for REAL order execution. When false, every exchange
    # adapter MUST run in MOCK mode and place no real orders, even if a user
    # has connected valid API keys. Default false until paper trading, the
    # safety layer, and the API vault are fully validated.
    live_trading_enabled: bool = False

    # --- Sprint 20 V11 platform feature flags ---
    # Canonical 20B flag (legacy `paper_trading` above stays for back-compat).
    paper_trading_enabled: bool = True
    exchange_api_vault_enabled: bool = True  # 20C: encrypted exchange-key vault
    mock_exchange_mode: bool = True  # adapters simulate fills, no real orders
    default_demo_balance: float = 10_000.0  # starting virtual balance per account
    # AES-256 master key for the exchange-credential vault (20C).
    # Blank -> derived from secret_key. Rotating this invalidates stored keys.
    vault_master_key: str = ""
    # 20D: global gate for the DEMO auto-trading engine (paper accounts only).
    # Distinct from auto_trading_enabled (LIVE, hard-locked false). Per-user
    # opt-in is AutoTradeConfig.enabled / PaperAccount.auto_follow.
    auto_trade_demo_enabled: bool = False
    # 20E: account-protection layer. On by default — wraps every auto open with
    # loss limits, correlation caps, cooldown, loss-streak, and kill switches.
    safety_layer_enabled: bool = True
    # 20F: mount the /api/live API. This only EXPOSES the endpoints (usable in
    # MOCK); placing REAL orders still requires live_trading_enabled=true AND
    # mock_exchange_mode=false. Default off.
    live_trading_api_enabled: bool = False
    # 20H: mount the ADMIN-only platform oversight API (/api/admin/*). Read-only
    # aggregation + user moderation; never exposes decrypted credentials. Default off.
    admin_dashboard_enabled: bool = False

    # ── Live Pilot — a tiny, gated, Binance-only live run (default OFF) ──
    # Never auto-executes: every real order needs the manual confirmation phrase
    # AND the live gate AND all safety checks. One designated user, BTC/ETH only.
    live_pilot_enabled: bool = False
    live_pilot_user_id: int = 0
    live_pilot_max_notional: float = 50.0
    live_pilot_max_positions: int = 2
    live_pilot_max_leverage: int = 3
    live_pilot_allowed_symbols: str = "BTCUSDT,ETHUSDT"
    live_pilot_require_confirmation: bool = True

    # --- Sprint 21 — Live Safety Foundation feature flags (all default OFF) ---
    # These engines are read-only / non-destructive by design; the flags only
    # gate whether their APIs + startup hooks are active. None of them place,
    # cancel, or modify real orders except emergency_close (admin-triggered,
    # reduce-only) which additionally requires the live execution gate.
    reconciliation_enabled: bool = False  # 21B: DB↔exchange drift detection API
    position_recovery_enabled: bool = False  # 21C: rebuild state on startup + API
    order_failure_engine_enabled: bool = False  # 21D: failure tracking + retry policy API
    accounting_enabled: bool = False  # 21E: net-PnL accounting API
    emergency_close_enabled: bool = False  # allow reduce-only emergency close action
    # When >0, a user who hits this many live-order failures inside the window
    # below has their auto-trading tripped by the circuit breaker.
    order_failure_breaker_threshold: int = 5
    order_failure_breaker_window_sec: int = 300
    tp_sl_retry_max: int = 3  # TP/SL placement retries before UNSAFE

    # --- Stop-Loss Engine V2 (previous-1D support/resistance stop) ---
    # When enabled and STOPLOSS_METHOD=PREV_1D_SUPPORT, the LONG stop is placed
    # below the previous completed 1D candle low (SHORT: above the prev 1D high)
    # plus an ATR buffer, with min/max distance guards. Disabled by default →
    # falls back to the legacy 15m ATR/structure stop. Only affects SL + TP/RR
    # derived from risk; signal/scanner/entry decisions are unchanged.
    stoploss_engine_v2_enabled: bool = False
    stoploss_method: str = "PREV_1D_SUPPORT"  # PREV_1D_SUPPORT | LEGACY
    stoploss_1d_buffer_atr_mult: float = 0.15  # buffer = mult * ATR(1D)
    min_sl_distance_percent: float = 2.0  # widen (or reject) if SL closer than this
    max_sl_distance_percent: float = 10.0  # reject signal if SL farther than this
    # When the 1D stop is closer than the min floor: "widen" to the floor, or "reject".
    stoploss_too_close_action: str = "widen"  # widen | reject

    # --- Sprint 21F — Binance live/testnet validation (read-only) ---
    # Gate the admin-only Binance preflight endpoint. The preflight is strictly
    # read-only (server time, balance, exchangeInfo, positions — never an order),
    # so it is safe to run before opening the live gate; default OFF regardless.
    binance_preflight_enabled: bool = False

    # --- Tier routing ---
    public_min_confidence: float = 75.0
    vip_min_confidence: float = 85.0
    elite_min_confidence: float = 95.0
    elite_min_rr: float = 2.5
    high_priority_confidence: float = 97.0
    high_priority_rr: float = 3.5
    public_chat_id: str = ""
    vip_chat_id: str = ""
    elite_vip_chat_id: str = ""

    # --- Funding Rate Engine (Sprint 11B) ---
    funding_enabled: bool = True
    funding_cache_seconds: int = 300
    funding_positive: float = 0.0003
    funding_negative: float = -0.0003
    funding_extreme_positive: float = 0.0008
    funding_extreme_negative: float = -0.0008
    funding_weight: int = 10

    # --- Sprint 20A: Auth / User accounts (SaaS, feature-flagged) ---
    auth_enabled: bool = False
    jwt_secret: str = ""  # falls back to secret_key if blank
    jwt_algorithm: str = "HS256"
    access_token_ttl_min: int = 30
    refresh_token_ttl_days: int = 14
    bcrypt_rounds: int = 12
    email_verification_required: bool = True
    account_lockout_threshold: int = 5  # failed logins before temporary lock
    account_lockout_minutes: int = 15
    app_base_url: str = "http://localhost:8010"
    auth_issuer: str = "Argus Quant"

    # ── P11 — Google OAuth (feature-flagged; default OFF, no secrets in code) ──
    google_oauth_enabled: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    oauth_success_redirect: str = "/app#/dashboard"
    oauth_failure_redirect: str = "/login?error=oauth_failed"
    # SMTP (optional — if smtp_host is blank, emails are logged, never sent)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "no-reply@alpharadar.local"
    smtp_tls: bool = True

    # --- Sprint 17: Liquidity Engine ---
    enable_liquidity_engine: bool = False

    # --- Sprint 18: Adaptive Threshold Engine ---
    adaptive_thresholds: bool = False
    adaptive_min_trades: int = 50  # minimum closed trades before adapting
    adaptive_lookback: int = 100  # analyze last N closed trades

    # --- Misc ---
    production_start_utc: str = ""
    timezone: str = "UTC"

    # ---------- helpers ----------
    @property
    def admin_ids(self) -> List[int]:
        if not self.telegram_admin_ids:
            return []
        out: List[int] = []
        for chunk in self.telegram_admin_ids.split(","):
            chunk = chunk.strip()
            if chunk.isdigit() or (chunk.startswith("-") and chunk[1:].isdigit()):
                out.append(int(chunk))
        return out

    @property
    def timeframes(self) -> List[str]:
        return [tf.strip() for tf in self.scan_timeframes.split(",") if tf.strip()]

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @property
    def jwt_signing_key(self) -> str:
        """JWT signing key: dedicated jwt_secret, else the global secret_key."""
        return (self.jwt_secret or self.secret_key or "").strip()

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host.strip())

    @property
    def vault_key_material(self) -> str:
        """Master secret for the exchange vault: dedicated key, else secret_key."""
        return (self.vault_master_key or self.secret_key or "").strip()

    @field_validator("min_confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not (0 <= v <= 100):
            raise ValueError("min_confidence must be 0..100")
        return v

    @field_validator("auto_trading_enabled")
    @classmethod
    def _no_live_trading(cls, v: bool) -> bool:
        # Auto-trading is architecture only — never allow live enabling
        if v:
            import warnings

            warnings.warn(
                "AUTO_TRADING_ENABLED=true is not supported yet — forced to false",
                stacklevel=2,
            )
        return False


def validate_startup(s: "Settings") -> None:
    """
    Called at startup. Raises SystemExit if critical env vars are missing.
    This ensures misconfigured deployments fail fast instead of silently.
    """
    errors: List[str] = []

    if not s.dashboard_password:
        errors.append(
            "DASHBOARD_PASSWORD is not set. "
            "Set a strong password in .env before exposing the dashboard."
        )

    if not s.secret_key:
        errors.append(
            "SECRET_KEY is not set. "
            'Generate a random secret: python -c "import secrets; print(secrets.token_hex(32))"'
        )

    if errors:
        print("=" * 60, file=sys.stderr)
        print("  ARGUS QUANT — STARTUP CONFIGURATION ERROR", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        for err in errors:
            print(f"  ❌  {err}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        sys.exit(1)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
