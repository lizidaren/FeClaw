"""
LocalStorage — 本地文件系统存储后端

所有路径映射规则:
    COS key:    feclaw/user_1/original/abc.jpg
    本地路径:   ./feclaw-storage/feclaw/user_1/original/abc.jpg
"""

import os
import logging
from datetime import datetime
from typing import Optional, List, Dict

from services.file_storage import FileStorage

logger = logging.getLogger(__name__)


class LocalStorage(FileStorage):
    """本地文件系统存储实现"""

    def __init__(self, root_dir: str = "./feclaw-storage"):
        self.root = os.path.abspath(root_dir)
        os.makedirs(self.root, exist_ok=True)
        logger.info(f"[LocalStorage] root={self.root}")

    def _resolve(self, key: str) -> str:
        """安全解析 key 到本地路径（防路径穿越）"""
        safe = key.lstrip("/").replace("\\", "/")
        path = os.path.realpath(os.path.join(self.root, safe))
        root_real = os.path.realpath(self.root)
        if not path.startswith(root_real + os.sep) and path != root_real:
            raise ValueError(f"Path traversal detected: {key}")
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
        with open(path, "wb") as f:
            f.write(file_bytes)

    def delete_file_by_key(self, key: str) -> bool:
        path = self._resolve(key)
        if not os.path.isfile(path):
            return False
        try:
            os.remove(path)
            return True
        except Exception as e:
            logger.error(f"[LocalStorage] delete failed: {key}, {e}")
            return False

    def list_objects(self, prefix: str, max_keys: int = 1000) -> Optional[List[Dict]]:
        dir_path = self._resolve(prefix)
        if not os.path.isdir(dir_path):
            return []
        results = []
        try:
            for root, dirs, files in os.walk(dir_path):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, self.root).replace("\\", "/")
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
