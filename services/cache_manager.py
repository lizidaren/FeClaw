"""
多层缓存系统 — MemoryCache + DiskCache + MetadataCache
为 VFS/COS 访问提供多级缓存加速，所有类线程安全
"""

import os
import time
import hashlib
import threading
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MemCacheEntry:
    data: bytes
    mtime: float
    size: int
    last_access: float = field(default_factory=time.time)
    access_count: int = 0


class MemoryCache:
    """
    LRU 内存缓存
    - 最大 64MB（可配置）
    - 文件 >10MB 不缓存到内存（直接走磁盘缓存或 COS）
    - 线程安全 (threading.RLock)
    - 可选 Redis 二级缓存（设置 redis_ttl > 0 启用）
    """

    def __init__(self, max_size_mb: int = 64, redis_ttl: int = 0):
        self.max_bytes = max_size_mb * 1024 * 1024
        self._cache: Dict[str, MemCacheEntry] = {}
        self._current_bytes = 0
        self._lock = threading.RLock()
        self.redis_ttl = redis_ttl
        self._redis_loop = None
        self._redis_thread = None

    def _redis_run(self, coro):
        """Run async Redis coroutine synchronously via a shared background event loop."""
        import asyncio
        try:
            if self._redis_loop is None:
                self._redis_loop = asyncio.new_event_loop()
                import threading
                self._redis_thread = threading.Thread(target=self._redis_loop.run_forever, daemon=True)
                self._redis_thread.start()
            future = asyncio.run_coroutine_threadsafe(coro, self._redis_loop)
            return future.result(timeout=10)
        except Exception:
            return None

    def get(self, key: str) -> Optional[bytes]:
        """获取缓存内容（更新 LRU）；Redis miss 时回退到内存"""
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                entry.last_access = time.time()
                entry.access_count += 1
                return entry.data

        # 内存未命中，尝试 Redis
        if self.redis_ttl > 0:
            data = self._redis_redis_get(key)
            if data:
                # 回填到内存缓存
                self.put(key, data)
                return data

        return None

    def put(self, key: str, data: bytes, mtime: float = None):
        """写入缓存（自动淘汰），同时写入 Redis（如果启用）"""
        if len(data) > 10 * 1024 * 1024:  # > 10MB，不缓存到内存
            return

        with self._lock:
            while self._current_bytes + len(data) > self.max_bytes:
                if not self._evict_one():
                    break

            entry = MemCacheEntry(
                data=data,
                mtime=mtime or time.time(),
                size=len(data),
                last_access=time.time(),
                access_count=1,
            )

            old = self._cache.get(key)
            if old:
                self._current_bytes -= old.size

            self._cache[key] = entry
            self._current_bytes += len(data)

        # 写入 Redis 二级缓存
        if self.redis_ttl > 0:
            self._redis_redis_set(key, data)

    def _evict_one(self) -> bool:
        """LRU 淘汰一个，返回是否成功淘汰"""
        if not self._cache:
            return False
        lru_key = min(self._cache, key=lambda k: self._cache[k].last_access)
        entry = self._cache.pop(lru_key)
        self._current_bytes -= entry.size
        return True

    def invalidate(self, key: str):
        """失效缓存（内存 + Redis）"""
        with self._lock:
            entry = self._cache.pop(key, None)
            if entry:
                self._current_bytes -= entry.size
        if self.redis_ttl > 0:
            self._redis_redis_delete(key)

    def invalidate_prefix(self, prefix: str):
        """失效匹配前缀的所有缓存（仅内存，Redis keys 不批量扫描）"""
        with self._lock:
            keys = [k for k in self._cache if k.startswith(prefix)]
            for k in keys:
                entry = self._cache.pop(k)
                self._current_bytes -= entry.size

    @property
    def size_mb(self) -> float:
        return self._current_bytes / (1024 * 1024)

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._cache)

    # ---- Redis helpers (private) ----

    def _redis_redis_get(self, key: str) -> Optional[bytes]:
        try:
            return self._redis_run(self._async_redis_get(key))
        except Exception:
            return None

    async def _async_redis_get(self, key: str) -> Optional[bytes]:
        from services.redis_client import _get_client, _make_key
        client = await _get_client()
        if client is None:
            return None
        return await client.get(_make_key(f"cache:{key}"))

    def _redis_redis_set(self, key: str, data: bytes):
        try:
            self._redis_run(self._async_redis_set(key, data))
        except Exception:
            pass

    async def _async_redis_set(self, key: str, data: bytes):
        from services.redis_client import _get_client, _make_key
        client = await _get_client()
        if client is None:
            return
        await client.setex(_make_key(f"cache:{key}"), self.redis_ttl, data)

    def _redis_redis_delete(self, key: str):
        try:
            self._redis_run(self._async_redis_delete(key))
        except Exception:
            pass

    async def _async_redis_delete(self, key: str):
        from services.redis_client import _get_client, _make_key
        client = await _get_client()
        if client is None:
            return
        await client.delete(_make_key(f"cache:{key}"))


class DiskCache:
    """
    本地磁盘缓存 — 二级缓存
    - 路径: /tmp/vfs-cache/{user_id}/{sha256(path)}
    - 最大 512MB（可配置）
    - LRU 淘汰（基于 atime）
    - 线程安全 (threading.RLock)
    """

    def __init__(self, path: str, max_size_mb: int = 512):
        self.root = path
        self.max_bytes = max_size_mb * 1024 * 1024
        os.makedirs(path, exist_ok=True)
        self._lock = threading.RLock()

    def get_path(self, key: str) -> str:
        """缓存文件路径"""
        hash_key = hashlib.sha256(key.encode()).hexdigest()
        return os.path.join(self.root, hash_key)

    def get(self, key: str) -> Optional[bytes]:
        """读取磁盘缓存"""
        cache_path = self.get_path(key)
        with self._lock:
            try:
                if not os.path.exists(cache_path):
                    return None
                os.utime(cache_path, None)
                with open(cache_path, "rb") as f:
                    return f.read()
            except Exception:
                return None

    def put(self, key: str, data: bytes):
        """写入磁盘缓存（自动淘汰）"""
        with self._lock:
            self._ensure_space(len(data))
            cache_path = self.get_path(key)
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "wb") as f:
                f.write(data)

    def _ensure_space(self, needed_bytes: int):
        """淘汰旧文件腾出空间"""
        current = self._get_total_size()
        if current + needed_bytes <= self.max_bytes:
            return

        files = []
        try:
            for fname in os.listdir(self.root):
                fpath = os.path.join(self.root, fname)
                if os.path.isfile(fpath):
                    files.append((fpath, os.path.getatime(fpath)))
        except Exception:
            return

        files.sort(key=lambda x: x[1])  # 按 atime 升序（最旧在前）

        for fpath, _ in files:
            if current + needed_bytes <= self.max_bytes:
                break
            try:
                size = os.path.getsize(fpath)
                os.unlink(fpath)
                current -= size
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.debug(f"[DiskCache] File cleanup error: {e}")

    def _get_total_size(self) -> int:
        total = 0
        try:
            for fname in os.listdir(self.root):
                fpath = os.path.join(self.root, fname)
                if os.path.isfile(fpath):
                    total += os.path.getsize(fpath)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug(f"[DiskCache] Size check error: {e}")
        return total

    def invalidate(self, key: str):
        """失效指定缓存"""
        cache_path = self.get_path(key)
        with self._lock:
            try:
                if os.path.exists(cache_path):
                    os.unlink(cache_path)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.debug(f"[DiskCache] Invalidation error for {key}: {e}")

    @property
    def size_mb(self) -> float:
        return self._get_total_size() / (1024 * 1024)


class MetadataCache:
    """
    目录列表 + 文件 stat 缓存
    - TTL=60s（可配置）
    - 写入操作后自动失效相关路径
    - 线程安全 (threading.RLock)
    """

    def __init__(self, ttl_seconds: int = 60):
        self.ttl = ttl_seconds
        self._dir_cache: Dict[str, tuple] = {}   # path → (timestamp, entries)
        self._stat_cache: Dict[str, tuple] = {}   # path → (timestamp, stat)
        self._lock = threading.RLock()

    def get_dir(self, path: str) -> Optional[List]:
        with self._lock:
            entry = self._dir_cache.get(path)
            if entry is None:
                return None
            ts, data = entry
            if time.time() - ts > self.ttl:
                del self._dir_cache[path]
                return None
            return data

    def set_dir(self, path: str, entries: List):
        with self._lock:
            self._dir_cache[path] = (time.time(), entries)

    def get_stat(self, path: str) -> Optional[dict]:
        with self._lock:
            entry = self._stat_cache.get(path)
            if entry is None:
                return None
            ts, data = entry
            if time.time() - ts > self.ttl:
                del self._stat_cache[path]
                return None
            return data

    def set_stat(self, path: str, stat: dict):
        with self._lock:
            self._stat_cache[path] = (time.time(), stat)

    def invalidate_dir(self, prefix: str):
        """失效匹配前缀的所有目录和 stat 缓存"""
        with self._lock:
            dir_keys = [k for k in self._dir_cache if k.startswith(prefix)]
            for k in dir_keys:
                del self._dir_cache[k]
            stat_keys = [k for k in self._stat_cache if k.startswith(prefix)]
            for k in stat_keys:
                del self._stat_cache[k]

    def invalidate_all(self):
        with self._lock:
            self._dir_cache.clear()
            self._stat_cache.clear()

    @property
    def dir_count(self) -> int:
        with self._lock:
            return len(self._dir_cache)


class WriteBuffer:
    """
    写入缓冲区（脏页管理）
    - 写入不立即写 COS（延迟 30 秒合并）
    - 短时间内同一文件的多次写入合并为一次 COS 请求
    - 定时器：每 30 秒批量刷脏页
    - 文件关闭时强制刷
    """

    def __init__(self, storage, meta_cache: MetadataCache, flush_interval: float = 30):
        self.storage = storage
        self.meta_cache = meta_cache
        self._dirty: Dict[str, bytes] = {}
        self._lock = threading.RLock()

    def write(self, cos_key: str, data: bytes):
        """写入缓冲区（不立即刷 COS）"""
        with self._lock:
            self._dirty[cos_key] = data
        # 失效元数据缓存
        parent = cos_key.rsplit("/", 1)[0] if "/" in cos_key else ""
        self.meta_cache.invalidate_dir(parent)

    def flush(self, cos_key: str = None):
        """刷脏页到 COS"""
        if cos_key:
            with self._lock:
                data = self._dirty.pop(cos_key, None)
            if data:
                self._upload_one(cos_key, data)
        else:
            with self._lock:
                keys = list(self._dirty.keys())
            for key in keys:
                with self._lock:
                    data = self._dirty.pop(key, None)
                if data:
                    self._upload_one(key, data)

    def flush_all(self):
        """强制刷所有脏页"""
        self.flush()

    def _upload_one(self, key: str, data: bytes):
        """上传单个文件到 COS"""
        try:
            self.storage.put_object(key, data)
        except Exception as e:
            logger.error(f"[WriteBuffer] 上传失败 {key}: {e}")
            with self._lock:
                self._dirty.setdefault(key, data)

    @property
    def dirty_count(self) -> int:
        with self._lock:
            return len(self._dirty)
