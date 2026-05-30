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

# Idempotent ALTER TABLE statements applied on every startup.
# Using IF NOT EXISTS so they are safe to run against both new and existing databases.
_SCHEMA_UPGRADES: list[str] = [
    # V3.1 — MTF layer score columns on signals
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS trend_score     FLOAT",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS structure_score FLOAT",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS setup_score     FLOAT",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_score     FLOAT",
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
    """Create tables and apply incremental schema upgrades."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in _SCHEMA_UPGRADES:
            await conn.execute(text(stmt))
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
