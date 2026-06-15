"""
Async Redis client — connection pool singleton with helper methods.

All operations are wrapped in try/except so the system works without Redis.
Uses key prefix "feclaw:" for namespace isolation.
"""

import asyncio
import logging
from typing import Optional

import redis.asyncio as aioredis

from config import settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "feclaw:"

_pool: Optional[aioredis.ConnectionPool] = None
_client: Optional[aioredis.Redis] = None
_loop_id: Optional[int] = None


def _make_key(key: str) -> str:
    return f"{KEY_PREFIX}{key}"


async def _get_client() -> Optional[aioredis.Redis]:
    """Lazily initialise and return the shared async Redis client.

    Recreates the client if the event loop has changed (e.g. between
    multiple ``asyncio.run()`` calls).
    """
    global _pool, _client, _loop_id

    try:
        current_loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        return None

    # If the event loop changed, reset the cached client
    if _client is not None and _loop_id != current_loop_id:
        _client = None
        if _pool:
            try:
                await _pool.disconnect()
            except Exception:
                pass
            _pool = None

    if _client is not None:
        return _client

    if not settings.REDIS_ENABLED:
        return None

    try:
        _pool = aioredis.ConnectionPool(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            username=settings.REDIS_USERNAME or None,
            password=settings.REDIS_PASSWORD or None,
            max_connections=10,
            decode_responses=False,
        )
        _client = aioredis.Redis(connection_pool=_pool)
        await _client.ping()
        _loop_id = current_loop_id
        logger.info("Redis connected: %s:%s/%s", settings.REDIS_HOST, settings.REDIS_PORT, settings.REDIS_DB)
        return _client
    except Exception:
        logger.warning("Redis unavailable — running without Redis")
        _client = None
        _loop_id = None
        if _pool:
            try:
                await _pool.disconnect()
            except Exception:
                pass
            _pool = None
        return None


async def get(key: str) -> Optional[bytes]:
    """Get a key from Redis."""
    try:
        client = await _get_client()
        if client is None:
            return None
        return await client.get(_make_key(key))
    except Exception:
        logger.debug("Redis GET failed for key=%s", key, exc_info=True)
        return None


async def set(key: str, value: bytes, ttl: int = 0) -> bool:
    """Set a key in Redis with optional TTL (seconds)."""
    try:
        client = await _get_client()
        if client is None:
            return False
        if ttl > 0:
            await client.setex(_make_key(key), ttl, value)
        else:
            await client.set(_make_key(key), value)
        return True
    except Exception:
        logger.debug("Redis SET failed for key=%s", key, exc_info=True)
        return False


async def delete(key: str) -> bool:
    """Delete a key from Redis."""
    try:
        client = await _get_client()
        if client is None:
            return False
        await client.delete(_make_key(key))
        return True
    except Exception:
        logger.debug("Redis DELETE failed for key=%s", key, exc_info=True)
        return False


async def incr(key: str, ttl: int = 0) -> Optional[int]:
    """Increment a counter. If ttl>0, set expiry (non-atomic but acceptable)."""
    try:
        client = await _get_client()
        if client is None:
            return None
        full_key = _make_key(key)
        val = await client.incr(full_key)
        if ttl > 0:
            await client.expire(full_key, ttl)
        return int(val)
    except Exception:
        logger.debug("Redis INCR failed for key=%s", key, exc_info=True)
        return None


async def decr(key: str) -> Optional[int]:
    """Decrement a counter (capped at 0, non-atomic but avoids SET race)."""
    try:
        client = await _get_client()
        if client is None:
            return None
        full_key = _make_key(key)
        val = await client.decr(full_key)
        return max(0, int(val))
    except Exception:
        logger.debug("Redis DECR failed for key=%s", key, exc_info=True)
        return None


async def expire(key: str, ttl: int) -> bool:
    """Set TTL on a key."""
    try:
        client = await _get_client()
        if client is None:
            return False
        return await client.expire(_make_key(key), ttl)
    except Exception:
        logger.debug("Redis EXPIRE failed for key=%s", key, exc_info=True)
        return False


async def is_available() -> bool:
    """Check if Redis is reachable."""
    return await _get_client() is not None


async def disconnect():
    """Gracefully close the connection pool."""
    global _pool, _client
    if _client:
        try:
            await _client.close()
        except Exception:
            pass
        _client = None
    if _pool:
        try:
            await _pool.disconnect()
        except Exception:
            pass
        _pool = None
    logger.info("Redis disconnected")
