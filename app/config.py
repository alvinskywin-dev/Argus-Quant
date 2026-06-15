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


def _norm_base_symbol(sym: str) -> str:
    """Upper-case a symbol and strip a trailing USDT/USD/BUSD/PERP quote so
    ``BTCUSDT`` -> ``BTC``. Used to match symbols to correlation groups."""
    s = (sym or "").strip().upper()
    for suffix in ("USDT", "BUSD", "USDC", "PERP", "USD"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


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

    # --- Telegram Community Consolidation (flagship public group) ---
    # During the engine-optimization / live-validation phase all signal flows are
    # merged into the single public flagship community (https://t.me/ArgusQuant).
    # When TELEGRAM_SINGLE_PUBLIC_GROUP is true, every signal routes to
    # PUBLIC_TELEGRAM_CHAT_ID and tier-specific (VIP/Elite/Premium) sends are off.
    # Multi-tier segmentation returns later via the *_routing_enabled flags.
    telegram_community_mode: bool = True
    telegram_single_public_group: bool = True
    public_telegram_chat_id: str = ""

    # Per-tier disable switches (default ON during consolidation). DEPRECATED env
    # vars VIP_TELEGRAM_CHAT_ID / ELITE_TELEGRAM_CHAT_ID / PREMIUM_TELEGRAM_CHAT_ID
    # are NOT deleted — they remain readable for backward compatibility but are
    # ignored while these *_disabled flags are true.
    vip_telegram_disabled: bool = True
    elite_telegram_disabled: bool = True
    premium_telegram_disabled: bool = True

    # Future-ready tier routing (default OFF; do not expose now). Re-enabling any
    # of these AND setting telegram_single_public_group=false restores the legacy
    # multi-tier broadcast behaviour without code changes.
    vip_routing_enabled: bool = False
    elite_routing_enabled: bool = False
    premium_routing_enabled: bool = False

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
    # Exclude pairs whose *base* asset is itself a stablecoin (e.g. USDCUSDT,
    # FDUSDUSDT). These hug 1.00 with near-zero volatility, so the scorer can
    # emit nonsense high-confidence signals on them. Quote is always USDT here.
    exclude_stablecoin_bases: bool = True
    stablecoin_bases: str = "USDC,FDUSD,TUSD,DAI,USDP,USDD,BUSD,USDE,GUSD,PYUSD,EURT,EURI,XUSD"

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
    # Flagship public community — used by the dashboard CTA / nav / hero buttons.
    telegram_channel_url: str = "https://t.me/ArgusQuant"
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

    # ── Live execution: risk-based sizing (#5) + slippage guard (#4) ──
    # Default risk-per-trade % used when a live order asks to be sized by risk
    # (entry→stop distance) instead of a raw notional.
    live_risk_per_trade_pct: float = 1.0
    # Cap on risk-based sizing: required margin must not exceed this fraction of
    # available balance (1.0 = up to full balance as margin).
    live_max_notional_frac: float = 1.0
    # Refuse / flag a MARKET entry when the live price has moved beyond this many
    # basis points adverse to the intended entry (50 bps = 0.5%). 0 disables.
    slippage_guard_enabled: bool = True
    max_slippage_bps: float = 50.0

    # ── Multi-user Live Beta — controlled live access (default OFF) ──
    live_beta_enabled: bool = False
    live_beta_max_users: int = 10
    live_beta_require_admin_approval: bool = True
    live_beta_invite_code: str = ""  # if set, required to request access
    live_beta_global_max_notional: float = 500.0  # cap across ALL beta users
    live_beta_default_user_max_notional: float = 100.0
    live_beta_default_max_positions: int = 2
    live_beta_per_symbol_max_notional: float = 100.0
    live_beta_allowed_exchanges: str = "binance"

    # --- Sprint 21 — Live Safety Foundation feature flags (all default OFF) ---
    # These engines are read-only / non-destructive by design; the flags only
    # gate whether their APIs + startup hooks are active. None of them place,
    # cancel, or modify real orders except emergency_close (admin-triggered,
    # reduce-only) which additionally requires the live execution gate.
    reconciliation_enabled: bool = False  # 21B: DB↔exchange drift detection API
    # Periodic background reconciliation sweep (DB↔exchange drift) while running.
    # Read-only: it only writes ReconciliationIssue audit rows and alerts admins;
    # it never opens/closes/cancels orders. Off by default.
    reconciliation_loop_enabled: bool = False
    reconciliation_interval_sec: int = 300  # how often the sweep runs (min 30)
    reconciliation_alert_critical: bool = True  # admin-alert on newly-found drift
    position_recovery_enabled: bool = False  # 21C: rebuild state on startup + API
    order_failure_engine_enabled: bool = False  # 21D: failure tracking + retry policy API
    accounting_enabled: bool = False  # 21E: net-PnL accounting API
    # Report performance PnL net of an estimated round-trip taker fee (#8) so
    # displayed avg/total PnL, profit factor, and Telegram stats reflect real
    # trading costs instead of gross price move. Set false to report gross.
    report_fees_enabled: bool = True
    report_roundtrip_fee_bps: float = 8.0  # 2 × 0.04% Binance USDT-M taker

    # ── HTTP API hardening ────────────────────────────────────────────
    # Per-IP fixed-window rate limit on the public API surface (abuse / DoS
    # protection). Applies to paths matching api_rate_limit_prefixes.
    api_rate_limit_enabled: bool = True
    api_rate_limit_per_min: int = 120
    api_rate_limit_prefixes: str = "/api/public/"  # comma-separated path prefixes
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

    # --- Stop-Loss Engine V3 (Balanced ATR/structure mode) ---
    # STOPLOSS_ENGINE_MODE selects the active stop engine and, when set, takes
    # priority over STOPLOSS_ENGINE_V2_ENABLED. The mandatory previous-1D stop
    # (PREV_1D_SUPPORT) placed the stop far from a 15m entry, inflating SL
    # distance and failing the RR filter on nearly every setup ("signal
    # starvation"). BALANCED derives the stop from the 15m ATR and the recent
    # 15m swing instead, keeping distance inside sane min/max bounds. The legacy
    # 15m ATR/structure stop and the prev-1D stop both remain available by mode.
    #   LEGACY_ATR      — legacy 15m swing/ATR stop (pre-V2 behaviour)
    #   PREV_1D_SUPPORT — Stop-Loss Engine V2 (previous-1D support/resistance)
    #   BALANCED        — V3 balanced ATR + structure stop (default)
    stoploss_engine_mode: str = "BALANCED"

    balanced_stop_atr_mult: float = 2.2  # ATR stop = entry ∓ ATR(15m) * mult
    balanced_stop_structure_buffer_atr_mult: float = 0.25  # swing ∓ ATR(15m) * mult
    balanced_stop_min_distance_percent: float = 1.8  # widen if SL closer than this
    balanced_stop_max_distance_percent: float = 8.0  # fallback/reject if farther
    # Only consult the prev-1D support/resistance as a last-resort candidate when
    # explicitly allowed; off by default so BALANCED never reintroduces the wide
    # 1D stop that caused starvation.
    balanced_stop_allow_1d_fallback: bool = False

    # Regime-adaptive max SL distance for BALANCED mode. Used only when the
    # Regime Adaptive Gate is enabled; otherwise BALANCED_STOP_MAX_DISTANCE_PERCENT
    # applies. Low-vol markets tolerate a wider stop; high-vol / sideways tighten.
    low_vol_balanced_max_distance_percent: float = 12.0
    high_vol_balanced_max_distance_percent: float = 6.0
    sideways_balanced_max_distance_percent: float = 6.0

    # ── Regime Adaptive Gate V1 (feature-flagged; default OFF) ──
    # Adapts the RR / SL-distance / confidence thresholds to the market regime so
    # that range/low-volatility markets (where the 1D SL sits far from a 15m
    # entry) are not blanket-rejected. NEVER forces emission — only relaxes or
    # tightens the gate within hard clamps. Per-regime values below; NORMAL is
    # the fallback for unknown regimes.
    regime_adaptive_gate_enabled: bool = False

    normal_min_rr: float = 1.5
    normal_max_sl_distance_percent: float = 10.0
    normal_min_confidence_delta: int = 0

    low_vol_min_rr: float = 1.0
    low_vol_max_sl_distance_percent: float = 15.0
    low_vol_min_confidence_delta: int = -3

    high_vol_min_rr: float = 1.8
    high_vol_max_sl_distance_percent: float = 8.0
    high_vol_min_confidence_delta: int = 3

    bull_min_rr: float = 1.3
    bull_max_sl_distance_percent: float = 12.0
    bull_min_confidence_delta: int = -2

    bear_min_rr: float = 1.3
    bear_max_sl_distance_percent: float = 12.0
    bear_min_confidence_delta: int = -2

    sideways_min_rr: float = 1.6
    sideways_max_sl_distance_percent: float = 8.0
    sideways_min_confidence_delta: int = 3
    # When the 1D stop is closer than the min floor: "widen" to the floor, or "reject".
    stoploss_too_close_action: str = "widen"  # widen | reject

    # ══════════════════════════════════════════════════════════════════
    #  SPRINT 22 — Institutional Risk & Execution Upgrade (all default OFF)
    #  Every engine below is feature-flagged and degrades gracefully: with
    #  its flag off, behaviour is identical to before. None place real
    #  orders; shadow mode is hard-guaranteed never to touch the exchange.
    # ══════════════════════════════════════════════════════════════════

    # ── 22A — Portfolio Exposure + Position Lock Engine ──
    portfolio_exposure_engine_enabled: bool = False
    max_open_positions_per_user: int = 5
    max_same_direction_positions: int = 3
    max_correlated_positions: int = 2
    max_daily_loss_percent: float = 5.0
    symbol_lock_enabled: bool = True
    pending_order_lock_enabled: bool = True
    # group_name:SYM,SYM;group_name:SYM,SYM  — symbols sharing a group are
    # treated as correlated. A leading group label before ':' is optional.
    correlation_groups: str = "BTC:ETH,SOL,DOGE,AVAX;AI:FET,AGIX,OCEAN;MEME:DOGE,SHIB,PEPE"

    # ── 22B — Signal Explainability ──
    signal_explainability_enabled: bool = False

    # ── 22C — Trade Lifecycle Analytics ──
    trade_lifecycle_analytics_enabled: bool = False

    # ── 22D — Break-Even + Partial TP Engine (reduce-only; never widens SL) ──
    break_even_engine_enabled: bool = False
    partial_tp_percent: float = 40.0  # % of size closed at TP1
    move_sl_to_entry_on_tp1: bool = True
    trailing_stop_enabled: bool = True
    trailing_stop_distance_percent: float = 1.5

    # ── 22E — News / Event Risk Filter ──
    news_event_filter_enabled: bool = False
    pre_event_block_minutes: int = 60
    post_event_block_minutes: int = 30
    high_impact_events: str = "CPI,FOMC,NFP,FED"

    # ── 22F — Liquidity Map V2 ──
    liquidity_map_v2_enabled: bool = False

    # ── 22G — Shadow Mode Live Validation (NEVER places real orders) ──
    shadow_mode_enabled: bool = False
    shadow_mode_slippage_bps: float = 5.0  # assumed slippage per fill (bps)
    shadow_mode_latency_ms: float = 250.0  # assumed execution latency

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
    # DEPRECATED (Community Consolidation): tier-specific chats are not used while
    # telegram_single_public_group is true. Kept for backward compatibility and
    # future multi-tier reactivation — do not delete.
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
    def stablecoin_base_set(self) -> set[str]:
        """Upper-cased set of base assets to exclude from the scan universe."""
        if not self.exclude_stablecoin_bases or not self.stablecoin_bases:
            return set()
        return {b.strip().upper() for b in self.stablecoin_bases.split(",") if b.strip()}

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
    def correlation_group_map(self) -> dict:
        """Parse CORRELATION_GROUPS into {GROUP: {SYM, ...}}.

        Accepts ``GROUP:SYM,SYM;GROUP:SYM,SYM``. A group label is optional —
        a bare ``SYM,SYM`` segment is given an auto label ``G0``, ``G1`` …
        Symbols are upper-cased and de-suffixed of a trailing USDT/USD/PERP so
        ``BTCUSDT`` and ``BTC`` land in the same group.
        """
        out: dict = {}
        raw = (self.correlation_groups or "").strip()
        if not raw:
            return out
        for idx, segment in enumerate(s for s in raw.split(";") if s.strip()):
            if ":" in segment:
                label, members = segment.split(":", 1)
                label = label.strip().upper() or f"G{idx}"
            else:
                label, members = f"G{idx}", segment
            syms = {_norm_base_symbol(m) for m in members.split(",") if m.strip()}
            # The group leader (e.g. ``BTC`` in ``BTC:ETH,SOL``) is itself a
            # member of the correlated set. Category labels like ``AI``/``MEME``
            # are added too but simply never match a real symbol.
            leader = _norm_base_symbol(label)
            if leader:
                syms.add(leader)
            if syms:
                out[label] = syms
        return out

    @property
    def high_impact_event_set(self) -> set:
        return {e.strip().upper() for e in (self.high_impact_events or "").split(",") if e.strip()}

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
