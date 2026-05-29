"""
Async Redis cache wrapper. Used by scanner to memoize klines and ticker data.
"""
from __future__ import annotations

from typing import Any, Optional

import orjson
import redis.asyncio as aioredis

from app.config import settings
from app.utils.logger import logger

_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=False,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
        try:
            await _redis.ping()
            logger.info("connected to redis")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"redis unreachable: {exc} — running without cache")
    return _redis


async def cache_set(key: str, value: Any, ttl: int = 60) -> None:
    try:
        r = await get_redis()
        await r.setex(key, ttl, orjson.dumps(value))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"cache_set fail {key}: {exc}")


async def cache_get(key: str) -> Any | None:
    try:
        r = await get_redis()
        v = await r.get(key)
        if v is None:
            return None
        return orjson.loads(v)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"cache_get fail {key}: {exc}")
        return None


async def shutdown_redis() -> None:
    global _redis
    if _redis is not None:
        try:
            await _redis.close()
        except Exception:  # noqa: BLE001
            pass
        _redis = None
