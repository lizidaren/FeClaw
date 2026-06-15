"""
令牌桶速率限制器
- 按字节计费的令牌桶
- 支持 sync 和 async consume
- 读限速 1MB/s，写限速 1MB/s
"""

import time
import threading
import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class TokenBucket:
    """
    令牌桶算法 — 限制文件读写速率

    Example:
        限制 1MB/s 上行:
        bucket = TokenBucket(rate=1_000_000, burst=2_000_000)
    """

    def __init__(self, rate: float, burst: float = None):
        """
        Args:
            rate: 令牌填充速率（字节/秒）
            burst: 最大突发量（字节），默认 = rate * 2
        """
        self.rate = rate
        self.burst = burst or rate * 2
        self.tokens = self.burst
        self.last_refill = time.monotonic()
        self._lock = threading.RLock()
        self._async_lock = None  # 懒初始化

    def _get_async_lock(self):
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def consume(self, tokens: int) -> bool:
        """
        同步消费 tokens 个令牌

        Returns:
            True: 允许操作
            False: 超速（会阻塞直到可用）
        """
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True

            # 令牌不够，等一会再试
            wait = (tokens - self.tokens) / self.rate
            time.sleep(min(wait, 0.1))

    async def async_consume(self, tokens: int) -> bool:
        """
        异步消费 tokens 个令牌

        Returns:
            True: 允许操作
        """
        lock = self._get_async_lock()
        while True:
            async with lock:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True

            wait = (tokens - self.tokens) / self.rate
            await asyncio.sleep(min(wait, 0.1))

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_refill = now

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill()
            return self.tokens


class RedisTokenBucket:
    """
    Redis-backed sliding-window rate limiter using Sorted Sets.

    Same interface as TokenBucket: ``consume`` / ``async_consume`` / ``available_tokens``.
    Falls back to always-allow when Redis is unavailable.

    Example:
        bucket = RedisTokenBucket(rate=1_000_000, burst=2_000_000, bucket_name="read")
    """

    def __init__(self, rate: float, burst: float = None, bucket_name: str = "default"):
        self.rate = rate
        self.burst = burst or rate * 2
        self.bucket_name = bucket_name
        self._window = 1.0

    def consume(self, tokens: int) -> bool:
        """
        Sync consume — bridges to async Redis via asyncio.run().
        If already inside a running event loop, falls back to always-allow.
        """
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return True
            return loop.run_until_complete(self.async_consume(tokens))
        except RuntimeError:
            try:
                return asyncio.run(self.async_consume(tokens))
            except Exception:
                return True
        except Exception:
            return True

    async def async_consume(self, tokens: int) -> bool:
        """
        Async consume — sliding-window check via Redis Sorted Set.
        Returns True if under the rate limit, False otherwise.
        """
        try:
            from services.redis_client import _get_client, _make_key

            client = await _get_client()
            if client is None:
                return True

            key = _make_key(f"ratelimit:{self.bucket_name}")
            now = time.time()
            window_start = now - self._window

            await client.zremrangebyscore(key, 0, window_start)
            await client.zadd(key, {f"{now}:{os.urandom(4).hex()}": tokens})
            entries = await client.zrange(key, 0, -1, withscores=True)
            total = sum(int(s) for _, s in entries)
            await client.expire(key, 2)

            return total <= self.rate
        except Exception:
            logger.debug("Redis rate-limit check failed, allowing", exc_info=True)
            return True

    @property
    def available_tokens(self) -> float:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            return self.rate
        try:
            return asyncio.run(self._async_available_tokens())
        except Exception:
            return self.rate

    async def _async_available_tokens(self) -> float:
        try:
            from services.redis_client import _get_client, _make_key

            client = await _get_client()
            if client is None:
                return self.rate

            key = _make_key(f"ratelimit:{self.bucket_name}")
            now = time.time()
            window_start = now - self._window
            await client.zremrangebyscore(key, 0, window_start)
            count = await client.zcard(key)
            return max(0, self.rate - count)
        except Exception:
            return self.rate
