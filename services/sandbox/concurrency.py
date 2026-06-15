"""并发控制 + 数据结构

- SandboxConfig, ExecResult, BackgroundTask: 数据类
- SandboxConcurrencyLimiter: 全局并发限制器（in-memory 回退）
- RedisSandboxConcurrencyLimiter: Redis-backed 全局并发限制器
- _global_concurrency_limiter: 默认实例（自动选择 Redis 或 in-memory）
- Token 管理: register/unregister/validate sandbox token (SQLite 跨进程共享)
"""
import asyncio
import concurrent.futures
import logging
import secrets
import sqlite3
import threading
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional

from config import settings

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class SandboxConfig:
    """沙箱配置"""
    memory_limit_mb: int = 128
    execution_timeout: int = 300
    max_processes: int = 10
    max_open_files: int = 64
    max_file_size: int = 100 * 1024 * 1024  # 100 MB


@dataclass
class ExecResult:
    """执行结果"""
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    sandbox_id: str = ""


@dataclass
class BackgroundTask:
    """后台任务"""
    id: str
    name: str
    process: subprocess.Popen
    port: Optional[int] = None
    output_buffer: deque = field(default_factory=lambda: deque(maxlen=1000))
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    sandbox_token: str = ""


# ============================================================================
# Global Concurrent Limiter
# ============================================================================


class SandboxConcurrencyLimiter:
    """全局沙箱并发限制器（最多 N 个并发）"""

    def __init__(self, max_concurrent: int = 5):
        self._max = max_concurrent
        self._running: Dict[str, float] = {}  # sandbox_id → started_at
        self._queue: deque = deque()  # (sandbox_id, event)
        self._lock = threading.RLock()

    def acquire(self, sandbox_id: str) -> bool:
        """尝试获取执行槽位，如果满了返回 False"""
        with self._lock:
            if len(self._running) < self._max:
                self._running[sandbox_id] = time.time()
                return True
            return False

    def release(self, sandbox_id: str):
        """释放执行槽位"""
        with self._lock:
            self._running.pop(sandbox_id, None)

    @property
    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    @property
    def max_concurrent(self) -> int:
        return self._max

    @property
    def queue_length(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def total_running(self) -> int:
        with self._lock:
            return len(self._running)


class RedisSandboxConcurrencyLimiter:
    """Redis-backed 全局沙箱并发限制器（跨进程共享计数器）"""

    _COUNTER_KEY = "sandbox:concurrency:count"

    def __init__(self, max_concurrent: int = 5):
        self._max = max_concurrent

    def _run_async(self, coro):
        """Bridge: run async coroutine synchronously.

        Works inside a running event loop by creating a new task:
        ``asyncio.ensure_future()`` followed by ``loop.run_until_complete()``
        is NOT possible when the loop is already running. Instead we
        use ``asyncio.run_coroutine_threadsafe()`` *from another thread*.

        If called from the main async thread (the normal FastAPI case),
        we create a one-shot nested event loop with ``asyncio.run()`` if
        the outer loop allows it; otherwise fall back to blocking-avoidance
        by queueing the coroutine on the running loop.
        """
        try:
            loop = asyncio.get_running_loop()
            # Running inside an event loop — schedule and block via
            # run_until_complete on a NEW event loop in the current thread.
            # (Python 3.12 allows nested event loops with asyncio.Runner)
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=30)
        except RuntimeError:
            # No running loop — safe to use run_until_complete
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        except Exception:
            logger.debug("_run_async failed, falling back", exc_info=True)
            return None

    def acquire(self, sandbox_id: str) -> bool:
        """尝试获取执行槽位，满了返回 False"""
        result = self._run_async(self._async_acquire())
        return result is True

    async def acquire_async(self, sandbox_id: str) -> bool:
        """异步版 acquire"""
        result = await self._async_acquire()
        return result is True

    async def _async_acquire(self) -> bool:
        try:
            from services.redis_client import _get_client, _make_key

            client = await _get_client()
            if client is None:
                return None  # signal fallback

            key = _make_key(self._COUNTER_KEY)
            # INCR-first: avoid TOCTOU race between GET+check+INCR
            new_val = await client.incr(key)
            if new_val == 1:
                await client.expire(key, 3600)
            if new_val > self._max:
                await client.decr(key)
                return False
            return True
        except Exception:
            logger.debug("Redis sandbox acquire failed", exc_info=True)
            return None

    def release(self, sandbox_id: str):
        """释放执行槽位"""
        self._run_async(self._async_release())

    async def release_async(self, sandbox_id: str):
        """异步版 release"""
        await self._async_release()

    async def _async_release(self):
        try:
            from services.redis_client import _get_client, _make_key

            client = await _get_client()
            if client is None:
                return

            key = _make_key(self._COUNTER_KEY)
            val = await client.decr(key)
            if val is not None and val <= 0:
                await client.delete(key)
        except Exception:
            logger.debug("Redis sandbox release failed", exc_info=True)

    @property
    def running_count(self) -> int:
        result = self._run_async(self._async_running_count())
        return result if result is not None else 0

    async def running_count_async(self) -> int:
        """异步版 running_count"""
        return await self._async_running_count()

    async def _async_running_count(self) -> int:
        try:
            from services.redis_client import _get_client, _make_key

            client = await _get_client()
            if client is None:
                return 0

            key = _make_key(self._COUNTER_KEY)
            val = await client.get(key)
            return int(val) if val else 0
        except Exception:
            return 0

    @property
    def max_concurrent(self) -> int:
        return self._max

    @property
    def queue_length(self) -> int:
        return 0

    @property
    def total_running(self) -> int:
        return self.running_count


def _create_global_limiter() -> SandboxConcurrencyLimiter:
    """Factory: try Redis-backed limiter first, fall back to in-memory."""
    max_conc = settings.SANDBOX_MAX_CONCURRENT

    if settings.REDIS_ENABLED:
        redis_limiter = RedisSandboxConcurrencyLimiter(max_concurrent=max_conc)
        # Test connectivity with a quick ping
        try:
            result = redis_limiter._run_async(redis_limiter._async_running_count())
            if result is not None:
                logger.info("Using Redis-backed sandbox concurrency limiter (max=%s)", max_conc)
                return redis_limiter
        except Exception:
            pass
        logger.info("Redis sandbox limiter unavailable, falling back to in-memory")

    logger.info("Using in-memory sandbox concurrency limiter (max=%s)", max_conc)
    return SandboxConcurrencyLimiter(max_concurrent=max_conc)


# 全局并发限制器
_global_concurrency_limiter = _create_global_limiter()


# ============================================================================
# Sandbox Token Management (UDS auth, SQLite-backed for cross-process sharing)
# ============================================================================

_TOKEN_DB_PATH = "/tmp/feclaw_sandbox_tokens.db"


def _ensure_token_db():
    """确保 token 数据库表存在"""
    conn = sqlite3.connect(_TOKEN_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS sandbox_tokens (token TEXT PRIMARY KEY, agent_hash TEXT)")
    conn.commit()
    conn.close()


def register_sandbox_token(agent_hash: str) -> str:
    """注册 sandbox token（SQLite 存储，跨进程共享）"""
    token = secrets.token_hex(16)
    key = agent_hash if agent_hash else "default"
    conn = sqlite3.connect(_TOKEN_DB_PATH)
    conn.execute("INSERT OR REPLACE INTO sandbox_tokens (token, agent_hash) VALUES (?, ?)", (token, key))
    conn.commit()
    conn.close()
    return token


def unregister_sandbox_token(token: str):
    """注销 sandbox token"""
    if not token:
        return
    conn = sqlite3.connect(_TOKEN_DB_PATH)
    conn.execute("DELETE FROM sandbox_tokens WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def validate_sandbox_token(agent_hash: str, token: str) -> bool:
    """验证 token 是否匹配 agent_hash"""
    if not agent_hash or not token:
        return False
    conn = sqlite3.connect(_TOKEN_DB_PATH)
    cursor = conn.execute("SELECT agent_hash FROM sandbox_tokens WHERE token = ?", (token,))
    row = cursor.fetchone()
    conn.close()
    return row is not None and row[0] == agent_hash


def _cleanup_expired_tokens():
    """清理所有过期 token（sandbox 完成后如有残留）"""
    conn = sqlite3.connect(_TOKEN_DB_PATH)
    conn.execute("DELETE FROM sandbox_tokens")
    conn.commit()
    conn.close()


# 确保 token 数据库在模块加载时已初始化
_ensure_token_db()
