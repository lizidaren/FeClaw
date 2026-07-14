"""
向量搜索服务
- Embedding: Qwen3 text-embedding-v4 (1024d) via DashScope OpenAI 兼容接口
- 存储: COS 向量存储桶，NumpyVecStorage 作为本地回退
"""

import asyncio
import contextlib
import functools
import json
import logging
import fcntl
import os
import re
import socket
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional

import httpx
import numpy as np

from config import settings
from services.model_registry import resolve as _reg_resolve

logger = logging.getLogger(__name__)

# Legacy bucket constant (kept for backward compat with scripts/tests)
VECTOR_BUCKET = "firstentrance-gzvec-1257148458"

# 向量维度
VECTOR_DIMENSION = 1024
# COS 向量存储域名 & IP（WSL DNS 兜底）
VECTOR_ENDPOINT = "vectors.ap-guangzhou.tencentcos.com"
VECTOR_ENDPOINT_SUFFIX = "." + VECTOR_ENDPOINT
VECTOR_IP = "169.254.1.83"

_is_wsl = "microsoft" in __import__("platform").uname().release.lower()
_original_getaddrinfo = socket.getaddrinfo


@contextlib.contextmanager
def _vector_dns_scope():
    """历史上用于 WSL 的 DNS patch，现在使用正确的 internal endpoint 后不再需要。"""
    yield


class _VectorClientWrapper:
    """CosVectorsClient 包装器：每次方法调用自动套上 _vector_dns_scope。

    WSL 下 COS SDK 无法解析自定义 DNS 域名，需要在建立连接时临时劫持 DNS。
    非 WSL 环境直接透传。
    """

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if _is_wsl and callable(attr):
            @functools.wraps(attr)
            def wrapped(*args, **kwargs):
                with _vector_dns_scope():
                    return attr(*args, **kwargs)
            return wrapped
        return attr


# Embedding API
EMBEDDING_API_URL_QWEN = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
EMBEDDING_MODEL = "text-embedding-v4"  # deprecated, use settings.MAIN_EMBEDDING_MODEL instead
# Embedding 限制
MAX_BATCH_SIZE = 10
MAX_TOKENS = 8000
MAX_RETRIES = 3
RETRY_DELAY = 1.0
# 文本 token 估算：保守取 1 token ≈ 2 chars
CHARS_PER_TOKEN = 2
MAX_CHARS = MAX_TOKENS * CHARS_PER_TOKEN


TEXTBOOK_SUBJECT_INDEXES = [
    "idx-public-chemistry-textbook",
    "idx-public-math-rja-textbook",
    "idx-public-math-xj-textbook",
    "idx-public-physics-textbook",
    "idx-public-biology-textbook",
    "idx-public-chinese-textbook",
    "idx-public-english-textbook",
    "idx-public-geography-textbook",
    "idx-public-politics-textbook",
]


# ═══════════════════════════════════════════════════════════════════════
# VectorStorage 抽象基类
# ═══════════════════════════════════════════════════════════════════════

class VectorStorage(ABC):
    """向量存储后端抽象"""

    @abstractmethod
    def ensure_index(self, index: str) -> None:
        """确保 index 存在，不存在则自动创建"""
        ...

    @abstractmethod
    def query(self, index: str, query_vec: List[float], top_k: int,
              filter: dict = None) -> List[Dict]:
        """向量查询，返回 [{key, score, metadata}]"""
        ...

    @abstractmethod
    def put(self, index: str, vectors: List[Dict]) -> None:
        """写入向量。vectors: [{key, data: {float32: [...]}, metadata: {...}}]"""
        ...

    @abstractmethod
    def delete(self, index: str, keys: List[str]) -> None:
        """按 key 删除向量"""
        ...

    @abstractmethod
    def list_keys_by_prefix(self, index: str, prefix: str) -> List[str]:
        """列出 index 中指定前缀的所有 key"""
        ...


# ═══════════════════════════════════════════════════════════════════════
# CosVectorStorage — 腾讯云 COS VectorBucket 后端
# ═══════════════════════════════════════════════════════════════════════

class CosVectorStorage(VectorStorage):
    """腾讯云 COS 向量存储后端"""

    # Class-level caches (shared across all instances)
    _bucket_cache: List[Dict] = []
    _bucket_cache_ts: float = 0.0
    _index_bucket_cache: Dict[str, str] = {}
    _cache_ttl: float = 30.0

    __slots__ = ('agent_hash', '_client')

    def __init__(self, agent_hash: str = None):
        self.agent_hash = agent_hash
        self._client = None

    # ----- COS Client -----

    def _get_client(self):
        """懒加载 COS Vector Client"""
        if self._client is not None:
            return self._client

        from qcloud_cos import CosConfig, CosVectorsClient

        config = CosConfig(
            Region="ap-guangzhou",
            SecretId=settings.TENCENT_COS_SECRET_ID,
            SecretKey=settings.TENCENT_COS_SECRET_KEY,
            Domain=VECTOR_ENDPOINT,
            Scheme="https",
        )
        with _vector_dns_scope():
            raw = CosVectorsClient(config)
        self._client = _VectorClientWrapper(raw) if _is_wsl else raw
        return self._client

    def _my_appid(self) -> str:
        """Get APPID from config."""
        return settings.TENCENT_COS_APPID

    # ----- ensure_index -----

    def ensure_index(self, index: str) -> None:
        """确保 index 存在，不存在则自动创建（1024d, float32, cosine）"""
        bucket = self._resolve_bucket_for_write(index)

        try:
            client = self._get_client()
            _, data = client.get_index(Bucket=bucket, Index=index)
            if data and isinstance(data, dict) and "indexName" in data:
                self._index_bucket_cache[index] = bucket
                return
        except Exception as e:
            if "not found" in str(e).lower():
                pass  # 不存在，正常走创建流程
            else:
                logger.error("ensure_index check_index failed: %s — retrying create", e)
                # 不确定是否真的不存在，但尝试创建不亏

        try:
            client = self._get_client()
            client.create_index(
                Bucket=bucket,
                Index=index,
                DataType="float32",
                Dimension=VECTOR_DIMENSION,
                DistanceMetric="cosine",
            )
            self._index_bucket_cache[index] = bucket
            logger.info("Created index %s in bucket %s", index, bucket)
        except Exception as e:
            err_lower = str(e).lower()
            if "already exists" in err_lower or "exist" in err_lower:
                self._index_bucket_cache[index] = bucket
                logger.info("Index %s already exists in bucket %s", index, bucket)
            else:
                logger.error("create_index %s in bucket %s failed: %s", index, bucket, e)
                raise  # 让调用方知道创建失败

    # ----- query -----

    def query(self, index: str, query_vec: List[float], top_k: int,
              filter: dict = None) -> List[Dict]:
        """COS 向量查询"""
        client = self._get_client()
        bucket = self._resolve_bucket(index)
        kwargs = {
            "Bucket": bucket,
            "Index": index,
            "QueryVector": {"float32": query_vec},
            "TopK": top_k,
            "ReturnDistance": True,
            "ReturnMetaData": True,
        }
        if filter:
            kwargs["Filter"] = filter
        _, resp_data = client.query_vectors(**kwargs)
        return self._parse_query_response(resp_data)

    def _parse_query_response(self, resp_data) -> List[dict]:
        """解析 COS query_vectors 的响应

        入参: {vectors: [{key, distance, metadata}]}
        返回: [{key, score(1-distance), metadata}]
        """
        if not resp_data or not isinstance(resp_data, dict):
            return []

        vectors = resp_data.get("vectors", [])
        results = []
        for v in vectors:
            metadata = v.get("metadata", {})
            # COS SDK returns metadata as string (JSON or Python repr), parse to dict
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    try:
                        import ast
                        metadata = ast.literal_eval(metadata)
                    except (ValueError, SyntaxError):
                        metadata = {}  # 解析失败时返回空 dict，避免下游 .get() 报错
            results.append({
                "key": v.get("key", ""),
                "score": max(0.0, min(1.0, 1.0 - v.get("distance", 0))),
                "metadata": metadata,
            })
        return results

    # ----- put -----

    def put(self, index: str, vectors: List[Dict]) -> None:
        """写入向量到 COS"""
        if not vectors:
            return
        bucket = self._resolve_bucket_for_write(index)
        self.ensure_index(index)

        client = self._get_client()
        try:
            client.put_vectors(
                Bucket=bucket,
                Index=index,
                Vectors=vectors,
            )
            logger.info("Indexed %d vectors to %s", len(vectors), index)
        except Exception as e:
            err_str = str(e)
            if "duplicate" in err_str.lower():
                logger.warning("Duplicate key in batch for %s, falling back to individual puts", index)
                success = 0
                for v in vectors:
                    try:
                        client.put_vectors(
                            Bucket=bucket,
                            Index=index,
                            Vectors=[v],
                        )
                        success += 1
                    except Exception as ve:
                        logger.error("Individual put_vectors failed for key=%s: %s", v["key"], ve)
                logger.info("Indexed %d/%d vectors to %s (fallback mode)", success, len(vectors), index)
            else:
                logger.error("COS put_vectors failed on index %s: %s", index, e)

    # ----- delete -----

    def delete(self, index: str, keys: List[str]) -> None:
        """从 COS 删除向量"""
        if not keys:
            return
        keys = list(dict.fromkeys(keys))
        client = self._get_client()
        bucket = self._resolve_bucket(index)
        client.delete_vectors(
            Bucket=bucket,
            Index=index,
            Keys=keys,
        )
        logger.info("Deleted %d vectors from %s", len(keys), index)

    # ----- list_keys_by_prefix -----

    def list_keys_by_prefix(self, index: str, prefix: str) -> List[str]:
        """列出 index 中指定前缀的所有 key"""
        client = self._get_client()
        bucket = self._resolve_bucket(index)
        _, data = client.list_objects(Bucket=bucket, Prefix=prefix)
        keys = []
        if data and isinstance(data, dict):
            for obj in data.get("Contents", []):
                key = obj.get("Key", "")
                if key.startswith(prefix):
                    keys.append(key)
        return keys

    # ----- Bucket Management (auto-scaling) -----

    def _list_vector_buckets(self) -> List[Dict]:
        """List ALL vector buckets. Cache with TTL to avoid excessive API calls.

        The legacy bucket `firstentrance-gzvec-1257148458` is always included
        even if it doesn't match the prefix (it has existing data).
        """
        cls = type(self)
        now = time.time()
        if now - cls._bucket_cache_ts < cls._cache_ttl and cls._bucket_cache:
            return cls._bucket_cache

        client = self._get_client()
        _, data = client.list_vector_buckets()
        buckets = data.get("VectorBuckets", data.get("vector_buckets", data.get("vectorBuckets", [])))

        # Ensure legacy bucket is included
        legacy = "firstentrance-gzvec-1257148458"
        def _bucket_name(b):
            return b.get("vectorBucketName", b.get("Name", b.get("name", "")))
        if not any(_bucket_name(b) == legacy for b in buckets):
            buckets.append({"Name": legacy, "Status": "Active"})

        cls._bucket_cache = buckets
        cls._bucket_cache_ts = now
        return buckets

    def _resolve_bucket(self, index: str) -> str:
        """Find which bucket an index lives in.

        Strategy:
        1. Check _index_bucket_cache
        2. Scan all vector buckets for this index (list_indexes on each)
        3. If not found, return the legacy bucket as default

        This is the core abstraction: callers never need to know the bucket.
        """
        cls = type(self)
        if index in cls._index_bucket_cache:
            return cls._index_bucket_cache[index]

        try:
            buckets = self._list_vector_buckets()
        except Exception as e:
            logger.warning("list_vector_buckets failed, falling back to legacy bucket: %s", e)
            buckets = []
        client = self._get_client()
        for b in buckets:
            name = b.get("vectorBucketName", b.get("Name", b.get("name", "")))
            if not name:
                continue
            try:
                _, data = client.list_indexes(Bucket=name, Prefix=index)
                indexes = data.get("indexes", data.get("Indexes", []))
                if any(i.get("IndexName", i.get("indexName", "")) == index for i in indexes):
                    cls._index_bucket_cache[index] = name
                    return name
            except Exception:
                continue

        # Not found anywhere -> default to legacy bucket
        legacy = "firstentrance-gzvec-1257148458"
        cls._index_bucket_cache[index] = legacy
        return legacy

    def _resolve_bucket_for_write(self, index: str) -> str:
        """Determine which bucket to write a new index into.

        Strategy:
        1. If index already exists somewhere -> use that bucket
        2. If agent_hash is set, try to colocate with sibling indexes (same agent)
        3. Otherwise, pick the least-loaded bucket (fewest indexes)
        4. If all buckets are near capacity, auto-create a new one
        """
        # 1. Check if already exists
        try:
            existing = self._resolve_bucket(index)
            client = self._get_client()
            _, data = client.get_index(Bucket=existing, Index=index)
            if data and isinstance(data, dict) and "indexName" in data:
                return existing
        except Exception:
            pass

        # 2. Agent colocation: check sibling indexes
        if self.agent_hash:
            siblings = [f"idx-{self.agent_hash}-kb", f"idx-{self.agent_hash}-conv"]
            for sib in siblings:
                if sib != index:
                    try:
                        bucket = self._resolve_bucket(sib)
                        client = self._get_client()
                        _, data = client.get_index(Bucket=bucket, Index=sib)
                        if data and isinstance(data, dict) and "indexName" in data:
                            logger.info("Colocating %s with sibling %s in bucket %s", index, sib, bucket)
                            return bucket
                    except Exception:
                        continue

        # 3. Pick least-loaded bucket
        return self._pick_least_loaded_bucket()

    def _pick_least_loaded_bucket(self) -> str:
        """Find bucket with most remaining capacity, or create new one if all full."""
        try:
            buckets = self._list_vector_buckets()
        except Exception as e:
            logger.warning("list_vector_buckets failed in _pick_least_loaded_bucket: %s", e)
            return "firstentrance-gzvec-1257148458"
        client = self._get_client()

        best_bucket = None
        best_count = float('inf')

        for b in buckets:
            name = b.get("vectorBucketName", b.get("Name", b.get("name", "")))
            if not name:
                continue
            try:
                _, data = client.list_indexes(Bucket=name)
                indexes = data.get("indexes", data.get("Indexes", []))
                count = len(indexes)
                if count < best_count:
                    best_count = count
                    best_bucket = name
            except Exception:
                continue

        # If all full or no bucket found, create new one
        if best_bucket is None or best_count >= settings.MAX_INDEXES_PER_BUCKET:
            best_bucket = self._create_next_bucket(buckets)

        return best_bucket

    def _create_next_bucket(self, existing_buckets: list = None) -> str:
        """Auto-create the next vector bucket. Returns new bucket name."""
        if existing_buckets is None:
            existing_buckets = self._list_vector_buckets()

        prefix = settings.VECTOR_BUCKET_PREFIX
        appid = self._my_appid()

        # Find the next available number
        existing_names = set()
        for b in existing_buckets:
            name = b.get("vectorBucketName", b.get("Name", b.get("name", "")))
            if name:
                existing_names.add(name)
        existing_names.add("firstentrance-gzvec-1257148458")

        n = 1
        while True:
            candidate = f"{prefix}-{n:02d}-{appid}"
            if candidate not in existing_names:
                break
            n += 1

        # Create the bucket
        client = self._get_client()
        try:
            client.create_vector_bucket(Bucket=candidate)
            logger.info("Created new vector bucket %s", candidate)
        except Exception as e:
            logger.error("Failed to create vector bucket %s: %s", e)
            return "firstentrance-gzvec-1257148458"

        # Invalidate cache
        cls = type(self)
        cls._bucket_cache_ts = 0

        return candidate

    def _invalidate_bucket_cache(self):
        """Force re-fetch of bucket list on next access."""
        type(self)._bucket_cache_ts = 0


# ═══════════════════════════════════════════════════════════════════════
# NumpyVecStorage local fallback helpers
# ═══════════════════════════════════════════════════════════════════════

# Index name validation（保留供 NumpyVecStorage 使用）
_INDEX_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]+$')


def _validate_index(index: str):
    """校验 index 名：长度 ≤ 100，只含字母数字_-"""
    if not index or len(index) > 100:
        raise ValueError(f"Invalid index name: {index!r} (empty or too long, max 100)")
    if not _INDEX_NAME_RE.match(index):
        raise ValueError(f"Invalid index name: {index!r} (only a-zA-Z0-9_- allowed)")


def _match_filter(metadata: dict, filter: dict) -> bool:
    """Python 侧 metadata 过滤"""
    for field, condition in filter.items():
        if isinstance(condition, dict):
            if "$in" in condition:
                if metadata.get(field) not in condition["$in"]:
                    return False
        else:
            if metadata.get(field) != condition:
                return False
    return True


# ═══════════════════════════════════════════════════════════════════════
# NumpyVecStorage — 纯文件系统 + numpy 向量存储后端
# ═══════════════════════════════════════════════════════════════════════

class NumpyVecStorage(VectorStorage):
    """纯文件系统 + numpy 向量存储后端（零 C 扩展、零数据库）"""

    VECTOR_ROOT = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "vectors"
    )

    def __init__(self):
        os.makedirs(self.VECTOR_ROOT, exist_ok=True)

    def _index_dir(self, index: str) -> str:
        """每个 index 一个目录"""
        _validate_index(index)
        return os.path.join(self.VECTOR_ROOT, index)

    def _lock_path(self, index: str) -> str:
        return os.path.join(self._index_dir(index), ".lock")

    def _entries_path(self, index: str) -> str:
        return os.path.join(self._index_dir(index), "entries.json")

    def _npy_path(self, index: str) -> str:
        return os.path.join(self._index_dir(index), "embeddings.npy")

    def _cleanup_tmp(self, index: str):
        """清理残留的 .tmp 文件（崩溃恢复）"""
        for f in (self._npy_path(index).replace('.npy', '.tmp.npy'), self._entries_path(index) + ".tmp"):
            if os.path.exists(f):
                os.remove(f)

    @contextlib.contextmanager
    def _read_lock(self, index: str):
        """查询时：共享锁。阻止写入，允许多读。"""
        fd = None
        try:
            fd = self._open_lock(index)
            fcntl.flock(fd, fcntl.LOCK_SH)
            yield
        finally:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    @contextlib.contextmanager
    def _write_lock(self, index: str):
        """写入时：独占锁。阻止读写。"""
        fd = self._open_lock(index)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _open_lock(self, index: str) -> int:
        """打开或创建锁文件，返回 fd"""
        idx_dir = self._index_dir(index)
        os.makedirs(idx_dir, exist_ok=True)
        lock_path = self._lock_path(index)
        if not os.path.exists(lock_path):
            open(lock_path, 'w').close()
        return os.open(lock_path, os.O_RDONLY)

    # ── ensure_index ────────────────────────────────────────────────

    def ensure_index(self, index: str) -> None:
        """创建 index 目录（幂等）"""
        self._cleanup_tmp(index)
        idx_dir = self._index_dir(index)
        if not os.path.exists(idx_dir):
            os.makedirs(idx_dir, exist_ok=True)
            with open(self._entries_path(index), 'w') as f:
                json.dump([], f)
            open(self._lock_path(index), 'w').close()

    # ── put ─────────────────────────────────────────────────────────

    def put(self, index: str, vectors: List[Dict]) -> None:
        """原子写入：标记旧 key 为 deleted，追加新向量，内联 compact"""
        import numpy as np
        if not vectors:
            return

        npy_path = self._npy_path(index)
        entries_path = self._entries_path(index)

        with self._write_lock(index):
            self._cleanup_tmp(index)

            entries = (json.loads(open(entries_path, 'rb').read())
                       if os.path.exists(entries_path) else [])
            old = np.load(npy_path) if os.path.exists(npy_path) else np.empty((0, VECTOR_DIMENSION), dtype=np.float32)

            new_vecs = np.array([v["data"]["float32"] for v in vectors], dtype=np.float32)
            new_keys = {v["key"] for v in vectors}

            for entry in entries:
                if entry["key"] in new_keys:
                    entry["deleted"] = True

            all_vecs = np.vstack([old, new_vecs])

            for v in vectors:
                entries.append({
                    "key": v["key"],
                    "metadata": v.get("metadata", {}),
                    "created_at": datetime.now().isoformat(),
                })

            # 内联 compact：墓碑超过活跃行时同步清理
            alive_count = sum(1 for e in entries if not e.get("deleted"))
            tomb_count = len(entries) - alive_count
            if tomb_count > alive_count and tomb_count > 50:
                alive_indices = [i for i, e in enumerate(entries) if not e.get("deleted")]
                all_vecs = all_vecs[alive_indices]
                entries = [entries[i] for i in alive_indices]

            # 原子写入：.tmp → os.rename
            tmp_npy = npy_path.replace('.npy', '.tmp.npy')
            tmp_json = entries_path + ".tmp"
            np.save(tmp_npy, all_vecs)
            with open(tmp_npy, "rb") as f:
                os.fsync(f.fileno())
            with open(tmp_json, 'w') as f:
                json.dump(entries, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_npy, npy_path)
            os.rename(tmp_json, entries_path)

            self._cleanup_tmp(index)

    # ── query ───────────────────────────────────────────────────────

    def query(self, index: str, query_vec: List[float], top_k: int,
              filter: dict = None) -> List[Dict]:
        """mmap 加载 .npy，numpy 批量余弦相似度，行号对齐 entries.json"""
        import numpy as np
        npy_path = self._npy_path(index)
        entries_path = self._entries_path(index)
        if not os.path.exists(npy_path):
            return []

        with self._read_lock(index):
            query_np = np.array(query_vec, dtype=np.float32)
            query_norm = np.linalg.norm(query_np)

            embeddings = np.load(npy_path, mmap_mode='r')
            norms = np.linalg.norm(embeddings, axis=1)
            dots = np.dot(embeddings, query_np)
            similarities = dots / (norms * query_norm + 1e-8)

            with open(entries_path, 'r') as f:
                entries = json.load(f)

            min_count = min(len(embeddings), len(entries))

            deleted = set()
            for i in range(min_count):
                if entries[i].get("deleted"):
                    deleted.add(i)

            indices = np.argsort(-similarities[:min_count], kind='stable')

            results = []
            for idx in indices:
                if len(results) >= top_k:
                    break
                if idx in deleted:
                    continue
                entry = entries[idx]
                if filter and not _match_filter(entry.get("metadata", {}), filter):
                    continue

                score = max(0.0, min(1.0, float(similarities[idx])))
                results.append({
                    "key": entry["key"],
                    "score": score,
                    "metadata": entry.get("metadata", {}),
                })

        return results

    # ── delete ──────────────────────────────────────────────────────

    def delete(self, index: str, keys: List[str]) -> None:
        """标记删除（墓碑），不实际删除文件"""
        if not keys:
            return

        entries_path = self._entries_path(index)
        if not os.path.exists(entries_path):
            return

        with self._write_lock(index):
            with open(entries_path, 'r') as f:
                entries = json.load(f)

            key_set = set(keys)
            changed = False
            for entry in entries:
                if entry["key"] in key_set and not entry.get("deleted"):
                    entry["deleted"] = True
                    changed = True

            if changed:
                tmp = entries_path + ".tmp"
                with open(tmp, 'w') as f:
                    json.dump(entries, f, ensure_ascii=False)
                os.rename(tmp, entries_path)
                self._cleanup_tmp(index)

    # ── list_keys_by_prefix ─────────────────────────────────────────

    def list_keys_by_prefix(self, index: str, prefix: str) -> List[str]:
        """前缀匹配（跳过已删除）"""
        entries_path = self._entries_path(index)
        if not os.path.exists(entries_path):
            return []

        with self._read_lock(index):
            with open(entries_path, 'r') as f:
                entries = json.load(f)

        return [
            e["key"] for e in entries
            if e["key"].startswith(prefix) and not e.get("deleted")
        ]


# ═══════════════════════════════════════════════════════════════════════
# Local storage fallback
# ═══════════════════════════════════════════════════════════════════════


def _get_local_storage() -> VectorStorage:
    """创建本地 Numpy 向量存储。"""
    try:
        return NumpyVecStorage()
    except Exception as e:
        raise RuntimeError(f"Local vector storage unavailable: {e}") from e


# ═══════════════════════════════════════════════════════════════════════
# VectorSearchService — 对外 API 层
# ═══════════════════════════════════════════════════════════════════════

class VectorSearchService:
    """向量搜索服务（对外 API 层）。"""

    __slots__ = ('agent_hash', 'storage')

    def __init__(self, agent_hash: str = None):
        self.agent_hash = agent_hash
        self.storage = self._create_storage()

    def _create_storage(self) -> VectorStorage:
        """根据配置创建存储后端。"""
        backend = settings.VECTOR_STORAGE_BACKEND or "cos"
        if backend == "numpy":
            return _get_local_storage()
        return CosVectorStorage(agent_hash=self.agent_hash)

    # ----- Index Management -----

    def ensure_index(self, index: str) -> None:
        """确保 index 存在（公开方法，供 agent_init_service 等使用）"""
        self.storage.ensure_index(index)

    def _get_index_name(self, prefix: str) -> str:
        """生成 index 名: idx-{agent_hash}-{prefix} 或 idx-{prefix}"""
        if self.agent_hash:
            return f"idx-{self.agent_hash}-{prefix}"
        return f"idx-{prefix}"

    def list_keys_by_prefix(self, index: str, prefix: str) -> List[str]:
        """列出 index 中指定前缀的所有 key（公开方法，供 vfs_indexer 等使用）"""
        return self.storage.list_keys_by_prefix(index, prefix)

    # ----- Embedding -----

    async def _call_embedding_api(self, texts: List[str]) -> Optional[List[List[float]]]:
        """调用 Embedding API（根据 MAIN_EMBEDDING_MODEL 自动选择 provider）"""
        emb_model = settings.MAIN_EMBEDDING_MODEL
        emb_info = _reg_resolve(emb_model)
        emb_provider = emb_info["provider"]
        api_key_attr = emb_info.get("api_key_attr", "")
        api_key = getattr(settings, api_key_attr, "") or os.getenv(api_key_attr, "")
        if not api_key:
            logger.error(f"{api_key_attr} not configured")
            return None

        # provider → endpoint
        if emb_provider == "qwen":
            api_url = EMBEDDING_API_URL_QWEN
        elif emb_provider == "zhipuai":
            base = emb_info.get("base_url", "https://open.bigmodel.cn/api/paas/v4")
            api_url = f"{base}/embeddings"
        else:
            logger.error(f"Unsupported embedding provider: {emb_provider}")
            return None

        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        api_url,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": emb_model,
                            "input": texts,
                            "dimensions": VECTOR_DIMENSION,
                        },
                    )

                    if resp.status_code == 429:
                        if attempt < MAX_RETRIES - 1:
                            logger.warning("Embedding API 429, retrying in %.1fs", RETRY_DELAY)
                            await asyncio.sleep(RETRY_DELAY)
                            continue
                        logger.error("Embedding API 429, exhausted retries")
                        return None

                    resp.raise_for_status()
                    data = resp.json()

                    embeddings = [item.get("embedding", []) for item in data.get("data", [])]
                    if not embeddings:
                        logger.error("Empty embedding response data")
                        return None
                    return embeddings

            except Exception as e:
                logger.warning("Embedding API call failed (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    logger.error("Embedding API exhausted retries")
                    return None

        return None

    async def embed(self, text: str) -> List[float]:
        """单条文本 → 1024d 向量"""
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]

        result = await self._call_embedding_api([text])
        return result[0] if result else []

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量向量化（最多 10 条一批）"""
        all_embeddings: List[List[float]] = []

        for i in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[i:i + MAX_BATCH_SIZE]
            batch = [t[:MAX_CHARS] if len(t) > MAX_CHARS else t for t in batch]

            result = await self._call_embedding_api(batch)
            if result:
                all_embeddings.extend(result)
            else:
                all_embeddings.extend([[] for _ in batch])

        return all_embeddings

    # ----- Search -----

    async def search(self, query: str, index: str = None, top_k: int = 5) -> List[dict]:
        """搜索相似内容

        1. embed(query) → vec
        2. 确定搜索哪些 index
           - 指定 index 则搜指定 index
           - 有 agent_hash：搜 idx-{hash}-kb + idx-public-kb
           - 无 agent_hash：搜 idx-public-kb
        3. 存储 query 搜索
        4. 按 score 降序返回 top_k
        """
        vec = await self.embed(query)
        if not vec:
            logger.warning("embed() returned empty vector for query=%r", query[:60])
            return []

        # 确定搜索 index 列表
        if index:
            indexes = [index]
        else:
            indexes = []
            if self.agent_hash:
                indexes.append(self._get_index_name("kb"))
                indexes.append(self._get_index_name("conv"))
            indexes.append("idx-public-kb")

        # 并行搜索所有 index
        tasks = [self._query_index(vec, idx, top_k) for idx in indexes]
        results_list = await asyncio.gather(*tasks)

        # 合并、标记来源、加权、排序、截断
        source_map = {
            self._get_index_name("kb"): "knowledge_base",
            self._get_index_name("conv"): "conversation_memory",
        }
        merged = []
        for idx, results in zip(indexes, results_list):
            source = source_map.get(idx, "unknown")
            for r in results:
                r["source"] = source
                # 根据来源加权
                if source == "public_knowledge":
                    r["score"] = min(1.0, r["score"] * 1.1)
                elif source == "conversation_memory":
                    r["score"] = r["score"] * 0.9
                merged.append(r)
        merged.sort(key=lambda x: x.get("score", 0), reverse=True)
        return merged[:top_k]

    async def search_with_rerank(
        self, query: str, index: str = None, top_k: int = 5, rerank: bool = True,
    ) -> List[dict]:
        """搜索 + 可选重排序

        Args:
            query: 查询文本
            index: 指定索引（None 则自动选择）
            top_k: 最终返回结果数
            rerank: 是否启用重排序（True 时先取 top_k*10 候选再用 RerankService 精排）

        Returns:
            排序后的文档列表，每条含 rerank_score（如果 rerank=True）
        """
        if rerank:
            # 先取 50 条候选，再用 reranker 精排到 top_k
            candidate_k = max(top_k, 50)
            candidates = await self.search(query, index=index, top_k=candidate_k)
            if not candidates:
                return []

            from services.rerank_service import RerankService

            reranker = RerankService()
            # 构造 documents 格式：{text, metadata, ...}
            docs = []
            for c in candidates:
                docs.append({
                    "text": c.get("metadata", {}).get("text", ""),
                    "metadata": c.get("metadata", {}),
                    "key": c.get("key", ""),
                    "score": c.get("score", 0),
                    "source": c.get("source", ""),
                })

            reranked = await reranker.rerank(query, docs, top_n=top_k)
            return reranked

        return await self.search(query, index=index, top_k=top_k)

    async def search_gaokao(self, query: str, top_k: int = 5) -> List[dict]:
        """搜索 idx-public-gaokao-kb（高考题库），带重排序

        如果 idx-public-gaokao-kb 不存在或为空，回退到 idx-public-kb 并按 source 过滤。
        """
        from services.rerank_service import RerankService

        # 尝试从 idx-public-gaokao-kb 搜索
        try:
            candidates = await self.search(query, index="idx-public-gaokao-kb", top_k=50)
            gaokao_candidates = [
                c for c in candidates
                if c.get("metadata", {}).get("source", "") in ("53-gaokao", "gaokao-bench", "gaokao")
            ]
            if gaokao_candidates:
                reranker = RerankService()
                docs = self._build_rerank_docs(gaokao_candidates)
                return await reranker.rerank(query, docs, top_n=top_k)
        except Exception as e:
            logger.warning("search_gaokao on idx-public-gaokao-kb failed: %s, falling back", e)

        # 回退：从 idx-public-kb 搜索并过滤 gaokao 来源
        candidates = await self.search(query, index="idx-public-kb", top_k=50)
        gaokao_candidates = [
            c for c in candidates
            if c.get("metadata", {}).get("source", "") in ("53-gaokao", "gaokao-bench", "gaokao")
        ]
        if not gaokao_candidates:
            logger.info("search_gaokao: no gaokao results found in idx-public-kb either")
            return []

        reranker = RerankService()
        docs = self._build_rerank_docs(gaokao_candidates)
        return await reranker.rerank(query, docs, top_n=top_k)

    @staticmethod
    def _build_rerank_docs(candidates: List[dict]) -> List[dict]:
        """Build rerank document list from search candidates."""
        return [
            {
                "text": c.get("metadata", {}).get("text", ""),
                "metadata": c.get("metadata", {}),
                "key": c.get("key", ""),
                "score": c.get("score", 0),
                "source": c.get("source", ""),
            }
            for c in candidates
        ]

    async def search_textbook(self, query: str, top_k: int = 5) -> List[dict]:
        """搜索 idx-public-textbook-kb（教材知识库），带重排序
        迁移完成后将完全使用 idx-public-textbook-kb，目前以 idx-public-kb 为回退
        """
        from services.rerank_service import RerankService

        try:
            candidates = await self.search(query, index="idx-public-textbook-kb", top_k=50)
            if candidates:
                reranker = RerankService()
                docs = self._build_rerank_docs(candidates)
                return await reranker.rerank(query, docs, top_n=top_k)
        except Exception as e:
            logger.warning("search_textbook: idx-public-textbook-kb failed, falling back: %s", e)

        candidates = await self.search(query, index="idx-public-kb", top_k=50)
        _GAOKAO_SOURCES = ("gaokao-bench",)
        textbook_candidates = [
            c for c in candidates
            if c.get("metadata", {}).get("source", "") not in _GAOKAO_SOURCES
        ]
        if not textbook_candidates:
            return []
        reranker = RerankService()
        docs = self._build_rerank_docs(textbook_candidates)
        return await reranker.rerank(query, docs, top_n=top_k)

    async def search_public(self, query: str, top_k: int = 5) -> List[dict]:
        """多源分层搜索：合并库 top_10 + 教材 top_50 + 高考 top_50 → 去重 → 重排序"""
        from services.rerank_service import RerankService

        tasks = [
            self.search(query, index="idx-public-kb", top_k=10),
            self.search(query, index="idx-public-textbook-kb", top_k=50),
            self.search(query, index="idx-public-gaokao-kb", top_k=50),
            self.search(query, index="idx-public-math-trends", top_k=50),
        ]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        all_docs = []
        seen = set()
        for results in results_list:
            if isinstance(results, Exception):
                logger.warning("search_public source failed: %s", results)
                continue
            for r in results:
                k = r.get("key", "")
                if k and k not in seen:
                    seen.add(k)
                    text = r.get("metadata", {}).get("text", "")
                    all_docs.append({
                        "text": text,
                        "key": r.get("key", ""),
                        "score": r.get("score", 0),
                        "source": r.get("source", ""),
                    })

        # 3. kaoxiang 考向结构化数据查询（独立 SQLite，不依赖主数据库）
        try:
            kd_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'kaoxiang.db')
            if os.path.exists(kd_path):
                import sqlite3
                conn = sqlite3.connect(kd_path)
                cursor = conn.cursor()
                like = f'%{query}%'
                cursor.execute(
                    'SELECT kaodian, exam_examples, exam_trend, exam_frequency, core_competency, topic_full '
                    'FROM kaoxiang_kaodian WHERE kaodian LIKE ? OR exam_trend LIKE ? OR topic_full LIKE ? LIMIT 10',
                    (like, like, like)
                )
                for row in cursor.fetchall():
                    name, examples_raw, trend, freq, competency, topic = row
                    examples = ', '.join(json.loads(examples_raw or '[]')[:5])
                    text = f'【考频数据】考点「{name}」近4年考频{freq}，核心素养{competency}，考向：{trend}。真题示例：{examples}'
                    key = f'kaoxiang-sqlite-{name}'
                    if key not in seen:
                        seen.add(key)
                        all_docs.append({
                            'text': text,
                            'key': key,
                            'score': 0.5,
                            'source': 'kaoxiang_sqlite',
                            'metadata': {'type': 'kaodian', 'source': 'kaoxiang_sqlite'},
                        })
                conn.close()
        except Exception as e:
            logger.warning('kaoxiang_sqlite search failed: %s', e)

        if not all_docs:
            return []

        reranker = RerankService()
        return await reranker.rerank(query, all_docs, top_n=top_k)

    async def search_quality_textbook(self, query: str, top_k: int = 50, agent_hash: str = None, min_score: float = 0.0) -> List[dict]:
        """Search all 9 high-quality per-subject textbook indexes + trends + agent KB. Parallel, merge, dedup, return."""
        vec = await self.embed(query)
        if not vec:
            return []
        tasks = [self._query_index(vec, idx, top_k) for idx in TEXTBOOK_SUBJECT_INDEXES]
        if agent_hash:
            tasks.append(self._query_index(vec, f"idx-{agent_hash}-kb", 10))
        tasks.append(self._query_index(vec, "idx-public-math-trends", top_k))

        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        merged = []
        seen = set()
        for results in results_list:
            if isinstance(results, Exception):
                logger.warning("search_quality_textbook source failed: %s", results)
                continue
            for r in results:
                k = r.get("key", "")
                score = r.get("score", 0)
                if score < min_score:
                    continue
                if k and k not in seen:
                    seen.add(k)
                    merged.append(r)

        return merged

    # ── Qwen subject routing ──────────────────────────────────────────
    _QWEN_ROUTING_PROMPT = """你是AI知识库的路由器。判断用户搜索内容属于哪个学科，分配搜索权重。

可用学科和对应索引：
- chemistry: 化学
- math: 数学（人教A版）
- math-xj: 数学（湘教版）
- physics: 物理
- biology: 生物
- chinese: 语文
- english: 英语
- geography: 地理
- politics: 政治

权重决定各学科搜索多少结果。总和可以不是1，但不要差异过大。

返回JSON：
{"routes": [{"index": "chemistry", "weight": 0.6}, {"index": "biology", "weight": 0.4}], "reasoning": "涉及葡萄糖，属于化学有机物和生物代谢"}

规则：
- 精确匹配（如"复数的三角形式"）→ 单一学科权重接近1
- 跨学科（如"葡萄糖"）→ 多学科合理分配
- 泛知识/常识（如"飞机"）→ 返回空routes列表，均衡搜索所有学科"""

    _ROUTE_TO_INDEX = {
        "chemistry": "idx-public-chemistry-textbook",
        "math": "idx-public-math-rja-textbook",
        "math-xj": "idx-public-math-xj-textbook",
        "physics": "idx-public-physics-textbook",
        "biology": "idx-public-biology-textbook",
        "chinese": "idx-public-chinese-textbook",
        "english": "idx-public-english-textbook",
        "geography": "idx-public-geography-textbook",
        "politics": "idx-public-politics-textbook",
    }

    async def _route_subjects(self, query: str) -> tuple:
        """用Qwen判断查询关联学科及权重。
        Returns: (routes, should_all)
        """
        from services.llm_service import llm_service

        messages = [
            {"role": "system", "content": self._QWEN_ROUTING_PROMPT},
            {"role": "user", "content": query},
        ]
        try:
            result = await llm_service.chat_json(
                messages=messages,
                provider=_reg_resolve("qwen3.6-flash")["provider"],
                model="qwen3.6-flash",
                disable_thinking=True,
                request_type="knowledge_router",
            )
            routes = result.get("routes", []) if isinstance(result, dict) else []
            if not routes:
                return [], True
            valid_routes = []
            valid_indexes = {
                "chemistry", "math", "math-xj", "physics", "biology",
                "chinese", "english", "geography", "politics",
            }
            for r in routes:
                if isinstance(r, dict) and r.get("index") in valid_indexes and isinstance(r.get("weight"), (int, float)):
                    valid_routes.append({"index": r["index"], "weight": float(r["weight"])})
            return valid_routes, False
        except Exception as e:
            logger.warning("_route_subjects failed: %s, fallback to all indexes", e)
            return [], True

    async def search_public_with_quality(self, query: str, top_k: int = 5, agent_hash: str = None, min_score: float = 0.0) -> List[dict]:
        """Multi-source quality search with Qwen-routed stratified sampling.
        Returns results suitable for prompt injection."""
        from services.rerank_service import RerankService

        # Run embedding and Qwen routing in parallel (independent tasks)
        vec_task = asyncio.create_task(self.embed(query))
        route_task = asyncio.create_task(self._route_subjects(query))

        vec = await vec_task
        if not vec:
            return []

        routes, should_all = await route_task
        # Qwen routing: determine which subjects to search and allocate budget

        TOTAL_BUDGET = 100   # total rerank candidate budget for subject indexes
        MIN_PER_INDEX = 3    # minimum when a subject has non-zero weight

        tasks = []
        task_labels = []
        source_map = {}

        if should_all or not routes:
            # Fallback: search all subjects evenly (small top_k to stay under 500 limit)
            for idx in TEXTBOOK_SUBJECT_INDEXES:
                tasks.append(self._query_index(vec, idx, 10))
                task_labels.append(idx)
                source_map[idx] = "textbook"
        else:
            # Stratified: allocate budget by Qwen weights
            total_weight = sum(r["weight"] for r in routes) or 1.0
            for route in routes:
                idx = self._ROUTE_TO_INDEX.get(route["index"])
                if not idx:
                    continue
                per_index = max(MIN_PER_INDEX, int(route["weight"] / total_weight * TOTAL_BUDGET))
                tasks.append(self._query_index(vec, idx, per_index))
                task_labels.append(idx)
                source_map[idx] = "textbook"

        # Always search gaokao and trends (cross-subject data)
        tasks.append(self._query_index(vec, "idx-public-math-trends", 20))
        task_labels.append("idx-public-math-trends")
        source_map["idx-public-math-trends"] = "math_trends"

        tasks.append(self._query_index(vec, "idx-public-gaokao-kb", 30))
        task_labels.append("idx-public-gaokao-kb")
        source_map["idx-public-gaokao-kb"] = "gaokao"

        # Agent private KB and conv
        if agent_hash:
            tasks.append(self._query_index(vec, f"idx-{agent_hash}-kb", 10))
            task_labels.append(f"idx-{agent_hash}-kb")
            source_map[f"idx-{agent_hash}-kb"] = "knowledge_base"
            tasks.append(self._query_index(vec, f"idx-{agent_hash}-conv", 10))
            task_labels.append(f"idx-{agent_hash}-conv")
            source_map[f"idx-{agent_hash}-conv"] = "conversation_memory"

        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge, filter by min_score, dedup
        all_docs = []
        seen = set()
        for idx_label, results in zip(task_labels, results_list):
            if isinstance(results, Exception):
                logger.warning("search_public_with_quality source %s failed: %s", idx_label, results)
                continue
            for r in results:
                score = r.get("score", 0)
                if score < min_score:
                    continue
                k = r.get("key", "")
                if k and k not in seen:
                    seen.add(k)
                    text = r.get("metadata", {}).get("text", "")
                    source = source_map.get(idx_label, "textbook")
                    all_docs.append({
                        "text": text,
                        "key": k,
                        "score": score,
                        "source": source,
                        "metadata": r.get("metadata", {}),
                    })

        if not all_docs:
            return []

        reranker = RerankService()
        reranked = await reranker.rerank(query, all_docs, top_n=top_k)
        return reranked[:top_k]

    async def search_memory(self, query: str, top_k: int = 3) -> List[dict]:
        """搜索 Agent 对话记忆（仅当有 agent_hash）"""
        if not self.agent_hash:
            return []

        vec = await self.embed(query)
        if not vec:
            return []

        idx = self._get_index_name("conv")
        return await self._query_index(vec, idx, top_k)

    async def _query_index(self, vec: List[float], index: str, top_k: int,
                           filter: dict = None, timeout: float = 15.0) -> List[dict]:
        """对单个 index 执行向量查询（统一包装 storage.query）"""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.storage.query, index, vec, top_k, filter),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning("_query_index timeout (%ss) on %s (top_k=%d)", timeout, index, top_k)
            return []
        except Exception as e:
            logger.error("query failed on index %s: %s", index, e)
            return []

    # ----- Index -----

    async def index_text(self, key: str, text: str, index: str, metadata: dict = None):
        """索引一条文本"""
        vec = await self.embed(text)
        if not vec:
            logger.warning("embed() empty for key=%s, skip index", key)
            return

        await self.index_batch([{"key": key, "text": text, "metadata": metadata or {}}], index)

    async def index_batch(self, items: List[dict], index: str):
        """批量索引多条 [{key, text, metadata}]"""
        texts = [item["text"] for item in items]
        vectors = await self.embed_batch(texts)

        cos_vectors = []
        for item, vec in zip(items, vectors):
            if not vec:
                continue
            meta = dict(item.get("metadata", {}))
            if "text" not in meta:
                meta["text"] = item["text"]
            cos_vectors.append({
                "key": item["key"],
                "data": {"float32": vec},
                "metadata": meta,
            })

        if not cos_vectors:
            return

        await asyncio.to_thread(self.storage.put, index, cos_vectors)

    # ----- Delete -----

    async def delete(self, keys: List[str], index: str):
        """删除向量数据"""
        if not keys:
            return
        await asyncio.to_thread(self.storage.delete, index, keys)
