"""
令牌桶速率限制器
- 按字节计费的令牌桶
- 支持 sync 和 async consume
- 读限速 1MB/s，写限速 1MB/s

FIX-A（审计 §6 N1 / H17）：另附 `SlidingWindowLimiter`（进程内固定窗口限流单点），
供 TOTP verify / 登录等「尝试次数」类限流复用 —— 原先散落在
`routers/feclaw_domain.py` 与 `routers/user.py`，导致 `POST /api/workspace/totp/verify`
孪生端点零限流。收敛成单点后所有调用方自动继承。

T18a：在 `SlidingWindowLimiter` 外面加了一层 :class:`RateLimiterGuard` **门面**，
把「后端可切换 + Redis 故障策略 + 统一入口」收敛到本模块：

- `RATE_LIMIT_BACKEND=memory|redis`：进程内 / 跨 worker 集中计数。
- `RATE_LIMIT_FAIL_MODE=open|closed`：Redis 不可用时
  `closed` ⇒ 抛 :class:`RateLimitUnavailable`（路由映射 503，**绝不静默放无限**）；
  `open` ⇒ 用进程内保守计数兜底并记 WARNING（仍不是无限）。
- 登录 / 改密 / 注册 / TOTP verify / share unlock **都走同一门面**，
  各自只是不同的实例与键前缀；不要在 router 里另外 new 一个限流器。
"""

import time
import threading
import asyncio
import logging
import os
from collections import defaultdict

logger = logging.getLogger(__name__)


class RateLimitUnavailable(RuntimeError):
    """限流后端不可用且策略要求 fail-closed（路由应返回 503）。"""


class SlidingWindowLimiter:
    """进程内固定窗口（滑动）限流器，线程安全。

    - 窗口内第 `max_attempts` 次起判定为受限（`is_limited` 返回 True）。
    - 桶超过 `clean_threshold` 时清理过期键，防内存膨胀。

    ⚠️ 多 worker 下每进程各自计数，阈值放大 N 倍；多 worker 部署请用
    `RATE_LIMIT_BACKEND=redis`（见 :class:`RateLimiterGuard`）。
    """

    def __init__(self, max_attempts: int, window_seconds: int, clean_threshold: int = 10000):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.clean_threshold = clean_threshold
        self._buckets = defaultdict(list)
        self._lock = threading.Lock()

    def is_limited(self, key: str) -> bool:
        """记录一次尝试；若该 key 在窗口内已满则返回 True（应拒绝）。"""
        now = time.time()
        with self._lock:
            bucket = [t for t in self._buckets.get(key, []) if now - t < self.window_seconds]
            if len(bucket) >= self.max_attempts:
                self._buckets[key] = bucket
                return True
            bucket.append(now)
            self._buckets[key] = bucket
            if len(self._buckets) > self.clean_threshold:
                stale = [k for k, v in list(self._buckets.items())
                         if all(now - t >= self.window_seconds for t in v)]
                for k in stale:
                    self._buckets.pop(k, None)
            return False

    def clear(self) -> None:
        """清空全部桶（测试/运维用）。"""
        with self._lock:
            self._buckets.clear()


# ────────────────────────────────────────────────────────────────────
# T18a：可切换后端的限流门面
# ────────────────────────────────────────────────────────────────────

_VALID_BACKENDS = ("memory", "redis")
_VALID_FAIL_MODES = ("open", "closed")

# Redis 同步客户端的进程内缓存。键 = 连接参数元组。
# 用**同步**客户端（而不是 services/redis_client 的 async 客户端）的原因：
# 登录 / TOTP verify 等调用点既可能在 async 视图里也可能是同步函数（
# `routers/feclaw_domain` 的部分调用点在同步代码路径），同步客户端不用
# 事件循环技巧也不会「在已有事件循环里 run_until_complete」，行为确定。
# 连接参数与键前缀来自 services/redis_client，保证同一命名空间。
_redis_client_cache = {}
_redis_cache_lock = threading.Lock()


def _reset_redis_client_cache() -> None:
    """测试/配置变更时清空缓存的 Redis 客户端。"""
    with _redis_cache_lock:
        for client in _redis_client_cache.values():
            try:
                client.close()
            except Exception:
                pass
        _redis_client_cache.clear()


def _get_sync_redis():
    """懒创建同步 Redis 客户端；不可用时返回 None（不抛）。

    测试可以 monkeypatch 本函数返回一个假客户端。
    """
    from config import settings

    if not getattr(settings, "REDIS_ENABLED", False):
        return None

    key = (
        settings.REDIS_HOST, int(settings.REDIS_PORT), int(settings.REDIS_DB),
        settings.REDIS_USERNAME or "", settings.REDIS_PASSWORD or "",
        float(getattr(settings, "RATE_LIMIT_REDIS_TIMEOUT", 0.5)),
    )
    with _redis_cache_lock:
        cached = _redis_client_cache.get(key)
        if cached is not None:
            return cached
        try:
            import redis as _redis  # 同步客户端

            redis_kwargs = dict(
                host=settings.REDIS_HOST,
                port=int(settings.REDIS_PORT),
                db=int(settings.REDIS_DB),
                username=settings.REDIS_USERNAME or None,
                password=settings.REDIS_PASSWORD or None,
                socket_timeout=float(getattr(settings, "RATE_LIMIT_REDIS_TIMEOUT", 0.5)),
                socket_connect_timeout=float(getattr(settings, "RATE_LIMIT_REDIS_TIMEOUT", 0.5)),
                decode_responses=True,
            )
            try:
                # 显式 RESP2：redis-py 8.x 默认协商 RESP3（HELLO 3），
                # 老版本 Redis / 受限代理会直接报 `unknown command HELLO`。
                client = _redis.Redis(protocol=2, **redis_kwargs)
            except TypeError:  # redis-py < 5 没有 protocol 参数
                client = _redis.Redis(**redis_kwargs)
            client.ping()
        except Exception as exc:
            logger.warning("[RateLimit] Redis 不可用: %s", exc)
            return None
        _redis_client_cache[key] = client
        return client


def _redis_incr(key: str, window_seconds: int):
    """固定窗口计数：返回窗口内计数，或 None（Redis 不可用 / 异常）。

    语义与进程内滑窗近似（同一窗口内累加，首次写入设过期）。
    原子性：`INCR` 本身原子；`EXPIRE` 用 `NX` 语义（ttl<0 才设），
    最坏情况是计数键不过期 —— 用 `max(window)` 的兜底过期时间收敛。
    """
    from config import settings

    try:
        # 复用 redis_client 的命名空间前缀（同一次部署的键名字典唯一）
        from services.redis_client import _make_key
    except Exception:  # pragma: no cover - redis 包缺失时仍可算出键名
        def _make_key(raw: str) -> str:
            return f"feclaw:{raw}"

    client = _get_sync_redis()
    if client is None:
        return None
    prefix = getattr(settings, "RATE_LIMIT_REDIS_PREFIX", "ratelimit:")
    full_key = _make_key(f"{prefix}{key}")
    try:
        count = int(client.incr(full_key))
        if count == 1:
            client.expire(full_key, max(1, int(window_seconds)))
        else:
            ttl = client.ttl(full_key)
            if ttl is None or int(ttl) < 0:
                client.expire(full_key, max(1, int(window_seconds)))
        return count
    except Exception as exc:
        logger.warning("[RateLimit] Redis 计数失败（%s）: %s", full_key, exc)
        _reset_redis_client_cache()
        return None


class RateLimiterGuard:
    """限流统一入口（T18a）。

    兼容 `SlidingWindowLimiter` 的旧接口（`is_limited` / `clear` /
    `max_attempts` / `window_seconds` / `_buckets`），因此既有调用点
    （`routers/user.py`、`routers/feclaw_domain.py`、`routers/workspace.py`）
    不需要改动就能拿到新后端，而 `tests/test_rate_limiter.py` 的
    `login_limiter is routers.user._login_limiter` 断言也继续成立。
    """

    def __init__(self, name: str, max_attempts: int, window_seconds: int,
                 attempts_setting: str = "", window_setting: str = ""):
        self.name = name
        self._memory = SlidingWindowLimiter(max_attempts=max_attempts, window_seconds=window_seconds)
        self._attempts_setting = attempts_setting
        self._window_setting = window_setting

    # ── 兼容属性 ──
    @property
    def max_attempts(self) -> int:
        if self._attempts_setting:
            value = getattr(_settings(), self._attempts_setting, None)
            if isinstance(value, int) and value > 0:
                return value
        return self._memory.max_attempts

    @property
    def window_seconds(self) -> int:
        if self._window_setting:
            value = getattr(_settings(), self._window_setting, None)
            if isinstance(value, int) and value > 0:
                return value
        return self._memory.window_seconds

    @property
    def _buckets(self):
        # 直接暴露进程内字典：`routers/feclaw_domain.py` 与既有测试按此清理
        return self._memory._buckets

    # ── 后端解析 ──
    @staticmethod
    def backend() -> str:
        raw = str(getattr(_settings(), "RATE_LIMIT_BACKEND", "memory") or "memory").strip().lower()
        if raw not in _VALID_BACKENDS:
            logger.warning("[RateLimit] 未知 RATE_LIMIT_BACKEND=%r，回退 memory", raw)
            return "memory"
        return raw

    @staticmethod
    def fail_mode() -> str:
        raw = str(getattr(_settings(), "RATE_LIMIT_FAIL_MODE", "closed") or "closed").strip().lower()
        if raw not in _VALID_FAIL_MODES:
            logger.warning("[RateLimit] 未知 RATE_LIMIT_FAIL_MODE=%r，按 closed 处理", raw)
            return "closed"
        return raw

    # ── 判定 ──
    def _sync_memory_limits(self) -> None:
        """把（可能被配置/测试改写过的）阈值同步到进程内桶。

        `RATE_LIMIT_*_ATTEMPTS/WINDOW` 是运行期可改的配置：不在这里同步，
        就会出现「属性报了新阈值、实际判定用旧阈值」的假配置。
        """
        try:
            self._memory.max_attempts = self.max_attempts
            self._memory.window_seconds = self.window_seconds
        except Exception:  # pragma: no cover - 配置不可读时保持原值
            pass

    def is_limited(self, key: str) -> bool:
        """记录一次尝试并判定是否受限。

        Raises:
            RateLimitUnavailable: Redis 后端不可用且 `RATE_LIMIT_FAIL_MODE=closed`
                （调用方应返回 503，**不要**吞掉异常变成放行）。
        """
        self._sync_memory_limits()
        backend = self.backend()
        if backend == "memory":
            return self._memory.is_limited(key)

        count = _redis_incr(f"{self.name}:{key}", self.window_seconds)
        if count is None:
            if self.fail_mode() == "closed":
                logger.error(
                    "[RateLimit] 后端 Redis 不可用且 FAIL_MODE=closed ⇒ 拒绝 %s 请求（503）",
                    self.name,
                )
                raise RateLimitUnavailable(f"rate limit backend unavailable ({self.name})")
            logger.warning(
                "[RateLimit] 后端 Redis 不可用且 FAIL_MODE=open ⇒ 用进程内计数兜底（%s）",
                self.name,
            )
            return self._memory.is_limited(key)
        return count > self.max_attempts

    async def ais_limited(self, key: str) -> bool:
        """异步入口：Redis 后端时把阻塞 IO 放到线程池，避免卡事件循环。"""
        self._sync_memory_limits()
        if self.backend() == "memory":
            return self._memory.is_limited(key)
        return await asyncio.to_thread(self.is_limited, key)

    def clear(self) -> None:
        self._memory.clear()


def _settings():
    """延迟导入 settings（避免模块导入期读 .env 的副作用影响测试替换）。"""
    from config import settings
    return settings


# 兼容别名：既有 import 名保持不变（tests/test_rate_limiter.py 断言同一对象）
def _make_limiter(name: str, attempts: int, window: int,
                  attempts_setting: str = "", window_setting: str = "") -> RateLimiterGuard:
    return RateLimiterGuard(
        name=name, max_attempts=attempts, window_seconds=window,
        attempts_setting=attempts_setting, window_setting=window_setting,
    )


# TOTP verify：IP + agent_hash 双维度（Q19/C2）
totp_verify_limiter = _make_limiter(
    "totp", 10, 300,
    attempts_setting="RATE_LIMIT_TOTP_ATTEMPTS",
    window_setting="RATE_LIMIT_TOTP_WINDOW",
)

# 登录：IP + username（H17 原有）
login_limiter = _make_limiter(
    "login", 10, 300,
    attempts_setting="RATE_LIMIT_LOGIN_ATTEMPTS",
    window_setting="RATE_LIMIT_LOGIN_WINDOW",
)
# 登录：IP 单桶（FIX-A 补，防用户名喷洒）；阈值放宽，避免误伤 NAT 后的多人。
login_ip_limiter = _make_limiter(
    "login_ip", 100, 300,
    attempts_setting="RATE_LIMIT_LOGIN_IP_ATTEMPTS",
    window_setting="RATE_LIMIT_LOGIN_WINDOW",
)

# 注册：IP 单桶（T18a：注册资源控制，防批量注册）
register_limiter = _make_limiter(
    "register", 5, 3600,
    attempts_setting="RATE_LIMIT_REGISTER_ATTEMPTS",
    window_setting="RATE_LIMIT_REGISTER_WINDOW",
)

# 改密：IP + user 双维度（T18a：密码操作走同一入口）
password_change_limiter = _make_limiter(
    "password", 10, 300,
    attempts_setting="RATE_LIMIT_PASSWORD_ATTEMPTS",
    window_setting="RATE_LIMIT_PASSWORD_WINDOW",
)

# 分享密码解锁（T18a：share unlock 与登录同入口）。
# ⚠️ 调用点在 `routers/share.py`（不属于 T18a 文件清单）—— 见跨界请求。
share_unlock_limiter = _make_limiter("share_unlock", 10, 300)


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
