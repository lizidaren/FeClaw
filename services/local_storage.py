"""
LocalStorage — 本地文件系统存储后端

路径映射规则:
    COS key (用户数据):  feclaw/user_1/original/abc.jpg
    本地路径:             LOCAL_STORAGE_ROOT/feclaw/user_1/original/abc.jpg

    COS key (/public/ 公共数据):  feclaw/public/feclaw/index.md
    本地路径:                      PUBLIC_STORAGE_ROOT/feclaw/public/feclaw/index.md
"""

import os
import logging
import time
from datetime import datetime
from typing import Optional, List, Dict

from services.file_storage import FileStorage
from services.portable_lock import lock_exclusive, unlock

logger = logging.getLogger(__name__)


def _canonical_fs_path(value: str) -> str:
    """把 realpath 结果归一成**可比较**的路径形式。

    Windows 上两个真实问题（S8 实测）：
    * `os.path.realpath()` 有时返回 `\\\\?\\C:\\...` 扩展前缀，而 base 不带前缀；
    * 8.3 短名（例如 `ADMINI~1`）与长名指向同一目录，但字符串不同。
    直接 `startswith` 比较会把合法路径误判为“路径穿越”。
    比较前统一去扩展前缀 + normcase + normpath；不改变返回给调用方的真实路径。
    """
    text = str(value)
    for prefix in ("\\\\?\\", "//?/", "\\\\.\\", "//./"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return os.path.normcase(os.path.normpath(text))


class LocalStorage(FileStorage):
    """本地文件系统存储实现

    /public/ 路径映射到独立的 public_root，便于权限控制和目录隔离。
    """

    def __init__(self, root_dir: str = "./feclaw-storage", public_root: str = "./feclaw-public"):
        # realpath：把 8.3 短名/符号链接归一，避免 relpath(full, base) 跨形式生成
        # `../../Administrator/...` 这类错误 key（S8 在 TEMP=ADMINI~1 下实测复现）。
        self.root = os.path.realpath(root_dir)
        self.public_root = os.path.realpath(public_root) if public_root else self.root
        os.makedirs(self.root, exist_ok=True)
        if self.public_root != self.root:
            os.makedirs(self.public_root, exist_ok=True)
            logger.info(f"[LocalStorage] public_root={self.public_root}")
        logger.info(f"[LocalStorage] root={self.root}")
        # 全局写锁文件（跨所有 LocalStorage 实例共享）
        self._lock_path = os.path.join(self.root, ".local_storage.write.lock")
        if not os.path.exists(self._lock_path):
            open(self._lock_path, "w").close()

    def _resolve_root(self, key: str) -> str:
        """根据 key 路径决定使用哪个根目录

        - /public/ 或 feclaw/public/ 开头的 key → public_root
        - 其他 → root
        """
        normalized = key.lstrip("/")
        if normalized.startswith("public/") or "/public/" in normalized:
            return self.public_root
        return self.root

    def _resolve(self, key: str) -> str:
        """安全解析 key 到本地路径（防路径穿越）"""
        safe = key.lstrip("/").replace("\\", "/")
        base = self._resolve_root(key)
        path = os.path.realpath(os.path.join(base, safe))
        base_real = os.path.realpath(base)
        # 比较用归一形式（去 Windows 扩展前缀 + normcase），否则并发/短名下会误报
        if (_canonical_fs_path(path) != _canonical_fs_path(base_real)
                and not _canonical_fs_path(path).startswith(
                    _canonical_fs_path(base_real) + os.sep)):
            raise ValueError(f"Path traversal detected: {key} (base={base})")
        return path

    def get_file_content(self, key: str) -> Optional[bytes]:
        path = self._resolve(key)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception as e:
            logger.error(f"[LocalStorage] read failed: {key}, {e}")
            return None

    def put_object(self, key: str, file_bytes: bytes) -> None:
        path = self._resolve(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 使用排他锁保护并发写入
        with open(self._lock_path, "w") as lock_f:
            lock_exclusive(lock_f.fileno())
            try:
                tmp_path = path + ".tmp"
                with open(tmp_path, "wb") as f:
                    f.write(file_bytes)
                    f.flush()
                    os.fsync(f.fileno())
                # Windows 限制：目标被读者打开时替换会抛 PermissionError；
                # 短暂退避重试，避免“读多写少”场景随机丢写（POSIX 上永不触发）。
                for attempt in range(3):
                    try:
                        # os.rename 在 Windows 目标已存在时抛 FileExistsError；
                        # os.replace 才是 POSIX 语义的原子覆盖。
                        os.replace(tmp_path, path)
                        break
                    except PermissionError:
                        if attempt >= 2:
                            raise
                        time.sleep(0.02 * (attempt + 1))
            finally:
                unlock(lock_f.fileno())

    def upload_file(self, file_bytes: bytes, key: str, max_retries: int = 3) -> str:
        """直接上传文件到本地存储。

        与 CosStorage.upload_file 接口对齐，供 /api/file/upload 共用。
        本地存储不需要重试，但保留参数以兼容调用方。
        """
        self.put_object(key, file_bytes)
        # 返回与 COS 等价的 URL 形式（仅占位，本地模式下不会被真正使用）
        return f"local://{key}"

    def delete_file_by_key(self, key: str) -> bool:
        path = self._resolve(key)
        if not os.path.isfile(path):
            return False
        with open(self._lock_path, "w") as lock_f:
            lock_exclusive(lock_f.fileno())
            try:
                os.remove(path)
                return True
            except Exception as e:
                logger.error(f"[LocalStorage] delete failed: {key}, {e}")
                return False
            finally:
                unlock(lock_f.fileno())

    def delete_file(self, url: str) -> bool:
        """删除文件 —— 与 CosStorage 接口对齐。

        接受完整 URL（COS 模式）或 key 字符串（本地模式）：
        - COS 模式：URL 形如 `https://bucket.cos.region.myqcloud.com/key`，从中提取 key
        - 本地模式：直接当作 key 处理
        """
        if not url:
            return False
        # 提取 key：URL 形如 https://bucket.cos.region.myqcloud.com/key
        if url.startswith(("http://", "https://")):
            from urllib.parse import urlparse
            try:
                parsed = urlparse(url)
                host = parsed.netloc
                key = parsed.path.lstrip("/")
                # 去掉可能的 bucket 前缀（upload 时 URL 不含 bucket）
                # 如果 host 含 bucket，路径可能以 /<bucket>/ 开头；这里只取 path 部分作为 key
                if not key:
                    return False
            except Exception:
                return False
        elif url.startswith("local://"):
            key = url[len("local://"):]
        else:
            key = url
        return self.delete_file_by_key(key)

    def list_objects(self, prefix: str, max_keys: int = 1000) -> Optional[List[Dict]]:
        dir_path = self._resolve(prefix)
        base = self._resolve_root(prefix)
        if not os.path.isdir(dir_path):
            return []
        results = []
        try:
            for root, dirs, files in os.walk(dir_path):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, base).replace("\\", "/")
                    stat = os.stat(full)
                    results.append({
                        "Key": rel,
                        "Size": stat.st_size,
                        "LastModified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    })
                    if len(results) >= max_keys:
                        return results
        except Exception as e:
            logger.error(f"[LocalStorage] list failed: {prefix}, {e}")
            return None
        return results

    def file_exists(self, key: str) -> Optional[Dict]:
        """检查文件是否存在并返回元数据"""
        path = self._resolve(key)
        if not os.path.exists(path):
            return None
        try:
            stat = os.stat(path)
            return {
                "exists": True,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "is_dir": os.path.isdir(path),
            }
        except Exception as e:
            logger.error(f"[LocalStorage] stat failed: {key}, {e}")
            return None
