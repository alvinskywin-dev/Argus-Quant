"""
Structured rotating-file logging using loguru. Imported once at startup,
then every module can use `from app.utils.logger import logger`.

Retention and size caps are configurable via:
  LOG_RETENTION_DAYS (default 30)
  LOG_MAX_SIZE_MB    (default 100)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

from app.config import settings

_INITIALIZED = False


def setup_logging() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return

    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)

    logger.remove()

    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level:<8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    retention = f"{settings.log_retention_days} days"
    max_size = f"{settings.log_max_size_mb} MB"

    # stdout
    logger.add(
        sys.stdout,
        level=settings.log_level.upper(),
        format=fmt,
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    # master app log — configurable retention & size
    logger.add(
        os.path.join(settings.log_dir, "app.log"),
        level=settings.log_level.upper(),
        format=fmt,
        rotation=max_size,
        retention=retention,
        compression="zip",
        enqueue=True,
    )

    # errors-only log — always keep 30 days regardless of setting
    errors_retention = f"{max(30, settings.log_retention_days)} days"
    logger.add(
        os.path.join(settings.log_dir, "errors.log"),
        level="ERROR",
        format=fmt,
        rotation="20 MB",
        retention=errors_retention,
        compression="zip",
        enqueue=True,
    )

    # per-subsystem logs — half the main size cap, 7 days minimum
    sub_size = f"{max(10, settings.log_max_size_mb // 2)} MB"
    sub_retention = f"{max(7, settings.log_retention_days // 4)} days"
    _subsystems = {
        "scanner.log":   "app.scanner",
        "telegram.log":  "app.telegram_bot",
        "database.log":  "app.database",
        "websocket.log": "app.market_data.ws_engine",
    }
    for filename, module_prefix in _subsystems.items():
        logger.add(
            os.path.join(settings.log_dir, filename),
            level="DEBUG",
            format=fmt,
            rotation=sub_size,
            retention=sub_retention,
            compression="zip",
            enqueue=True,
            filter=lambda r, p=module_prefix: r["name"].startswith(p),
        )

    _INITIALIZED = True


setup_logging()

__all__ = ["logger", "setup_logging"]
