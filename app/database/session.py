"""
Async SQLAlchemy engine + session factory.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.database.models import Base
from app.utils.logger import logger

# Idempotent DDL statements applied on every startup.
# All statements use IF NOT EXISTS so they are safe to run against both
# new and existing databases.
_SCHEMA_UPGRADES: list[str] = [
    # V3.1 — MTF layer score columns on signals
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS trend_score     FLOAT",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS structure_score FLOAT",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS setup_score     FLOAT",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_score     FLOAT",
    # V3.1 Sprint 6 — paper_positions gains tp2 and tp3 columns
    "ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS tp2 FLOAT DEFAULT 0",
    "ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS tp3 FLOAT DEFAULT 0",
    # V3.1 Sprint 3 — DB-level active-signal uniqueness per symbol.
    # Prevents duplicate OPEN signals at the database layer as a last resort.
    # Uses a partial index (PostgreSQL-specific) so only active rows are constrained.
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_active_signal_symbol
       ON signals(symbol)
       WHERE status IN ('OPEN', 'ACTIVE', 'PENDING')""",
    # Sprint 16A — signal diagnostics JSON
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS diagnostics TEXT",
    "ALTER TABLE archive_signals ADD COLUMN IF NOT EXISTS diagnostics TEXT",
    # Sprint 16C — dynamic RR method tracking
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS rr_method VARCHAR(32)",
    "ALTER TABLE archive_signals ADD COLUMN IF NOT EXISTS rr_method VARCHAR(32)",
    # Sprint 19A — market regime classification at signal creation time
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS market_regime VARCHAR(32)",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS regime_score INTEGER",
    "ALTER TABLE archive_signals ADD COLUMN IF NOT EXISTS market_regime VARCHAR(32)",
    "ALTER TABLE archive_signals ADD COLUMN IF NOT EXISTS regime_score INTEGER",
    # Sprint 20D — auto-trading engine marks the positions it manages, and
    # records which break-even/trailing adjustment has been applied.
    "ALTER TABLE paper_account_positions ADD COLUMN IF NOT EXISTS auto_managed BOOLEAN DEFAULT false",
    "ALTER TABLE paper_account_positions ADD COLUMN IF NOT EXISTS protection VARCHAR(16)",
    # V11 audit — index signals.status for status-filtered scans (dashboard,
    # active-signal summary). Non-unique; complements uq_active_signal_symbol.
    "CREATE INDEX IF NOT EXISTS ix_signals_status ON signals(status)",
    # ── Sprint 21A — exchange permission validation outcome columns ──
    "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS can_read BOOLEAN DEFAULT false",
    "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS last_validation_status VARCHAR(24)",
    "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS permission_warning VARCHAR(256)",
    # ── Sprint 21B/21C — live_positions safety + recovery columns ──
    "ALTER TABLE live_positions ADD COLUMN IF NOT EXISTS take_profit FLOAT",
    "ALTER TABLE live_positions ADD COLUMN IF NOT EXISTS stop_loss FLOAT",
    "ALTER TABLE live_positions ADD COLUMN IF NOT EXISTS tp_sl_status VARCHAR(16) DEFAULT 'UNKNOWN'",
    "ALTER TABLE live_positions ADD COLUMN IF NOT EXISTS requires_review BOOLEAN DEFAULT false",
    "ALTER TABLE live_positions ADD COLUMN IF NOT EXISTS unsafe_reason VARCHAR(256)",
    "ALTER TABLE live_positions ADD COLUMN IF NOT EXISTS recovered_at TIMESTAMPTZ",
    "ALTER TABLE live_positions ADD COLUMN IF NOT EXISTS last_reconciled_at TIMESTAMPTZ",
    # ── Sprint 21 — indexes for the new tables + hot lookups ──
    "CREATE INDEX IF NOT EXISTS ix_live_positions_user ON live_positions(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_live_positions_exchange ON live_positions(exchange)",
    "CREATE INDEX IF NOT EXISTS ix_live_positions_status ON live_positions(status)",
    "CREATE INDEX IF NOT EXISTS ix_live_orders_position ON live_orders(exchange_order_id)",
    "CREATE INDEX IF NOT EXISTS ix_live_orders_status ON live_orders(status)",
    "CREATE INDEX IF NOT EXISTS ix_recon_issues_user ON reconciliation_issues(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_recon_issues_resolved ON reconciliation_issues(resolved)",
    "CREATE INDEX IF NOT EXISTS ix_order_failures_user ON order_failures(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_order_failures_final ON order_failures(final_state)",
    "CREATE INDEX IF NOT EXISTS ix_trade_acct_user ON live_trade_accounting(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_daily_pnl_user ON daily_user_pnl(user_id)",
    # ── Timezone System V1 — per-user display timezone (DB stays UTC) ──
    "ALTER TABLE auth_users ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) DEFAULT 'UTC'",
    "UPDATE auth_users SET timezone = 'UTC' WHERE timezone IS NULL",
    # ── P11 — Google OAuth identity columns on auth_users ──
    "ALTER TABLE auth_users ADD COLUMN IF NOT EXISTS provider VARCHAR(16) DEFAULT 'email'",
    "ALTER TABLE auth_users ADD COLUMN IF NOT EXISTS provider_user_id VARCHAR(64)",
    "ALTER TABLE auth_users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(512)",
    "UPDATE auth_users SET provider = 'email' WHERE provider IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_auth_users_provider_uid ON auth_users(provider_user_id)",
]

engine = create_async_engine(
    settings.postgres_dsn,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def init_db() -> None:
    """
    Create tables and apply incremental schema upgrades.

    Each upgrade statement is run individually.  Failures are logged as
    warnings rather than crashing startup — some upgrades (e.g. the partial
    unique index) require clean data and must be applied manually after a
    deduplication migration has run.
    """
    # Sprint 21 — register the live-safety models on Base.metadata so
    # create_all builds their tables. Imported here (not at module load) to
    # avoid import cycles with app.database.models.
    try:
        import app.accounting.models  # noqa: F401
        import app.order_failures.models  # noqa: F401
        import app.reconciliation.models  # noqa: F401
    except Exception as exc:  # noqa: BLE001 — missing optional module is non-fatal
        logger.warning(f"sprint21 model import skipped: {exc!s:.160}")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    for stmt in _SCHEMA_UPGRADES:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
        except Exception as exc:
            # Non-fatal: log and continue.  The index/column may already exist
            # or the data may need cleaning up first.
            logger.warning(f"schema upgrade skipped (non-fatal): {exc!s:.200} | SQL: {stmt[:80]}")

    logger.info("database initialized")


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def shutdown_db() -> None:
    await engine.dispose()
    logger.info("database engine disposed")
