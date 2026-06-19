"""
LocalStorage — 本地文件系统存储后端

路径映射规则:
    COS key (用户数据):  feclaw/user_1/original/abc.jpg
    本地路径:             LOCAL_STORAGE_ROOT/feclaw/user_1/original/abc.jpg

    COS key (/public/ 公共数据):  feclaw/public/feclaw/index.md
    本地路径:                      PUBLIC_STORAGE_ROOT/feclaw/public/feclaw/index.md
"""

import os
import fcntl
import logging
from datetime import datetime
from typing import Optional, List, Dict

from services.file_storage import FileStorage

logger = logging.getLogger(__name__)


class LocalStorage(FileStorage):
    """本地文件系统存储实现

    /public/ 路径映射到独立的 public_root，便于权限控制和目录隔离。
    """

    def __init__(self, root_dir: str = "./feclaw-storage", public_root: str = "./feclaw-public"):
        self.root = os.path.abspath(root_dir)
        self.public_root = os.path.abspath(public_root) if public_root else self.root
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
        if not path.startswith(base_real + os.sep) and path != base_real:
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
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                tmp_path = path + ".tmp"
                with open(tmp_path, "wb") as f:
                    f.write(file_bytes)
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(tmp_path, path)
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    def delete_file_by_key(self, key: str) -> bool:
        path = self._resolve(key)
        if not os.path.isfile(path):
            return False
        with open(self._lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                os.remove(path)
                return True
            except Exception as e:
                logger.error(f"[LocalStorage] delete failed: {key}, {e}")
                return False
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

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
