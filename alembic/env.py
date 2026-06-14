"""Alembic migration environment (async SQLAlchemy + asyncpg).

The database URL and the target metadata come from the application itself, so
migrations always match the running app's models and DSN. All model modules are
imported up front so every table is registered on ``Base.metadata`` (the
sprint-21 models live in separate packages and are otherwise only imported lazily
by init_db).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config import settings
from app.database.models import Base

# Register every model module so all tables are on Base.metadata.
for _mod in (
    "app.accounting.models",
    "app.execution.live_beta.models",
    "app.order_failures.models",
    "app.reconciliation.models",
):
    try:
        __import__(_mod)
    except Exception:  # noqa: BLE001 — an optional module must not break migrations
        pass

config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # noqa: BLE001 — logging config is best-effort
        pass

# Single source of truth for the DSN.
config.set_main_option("sqlalchemy.url", settings.postgres_dsn)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without a DB connection (`alembic upgrade head --sql`)."""
    context.configure(
        url=settings.postgres_dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": settings.postgres_dsn},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
