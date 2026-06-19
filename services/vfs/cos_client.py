"""
COS client for VirtualFileSystem - COS 的 get/put/delete/list 封装
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class CosClient:
    """
    封装 COS 存储操作，提供统一的 get/put/delete/list 接口
    """

    def __init__(self, storage_service=None):
        self._storage = storage_service

    @property
    def storage(self):
        """懒加载 StorageService"""
        if self._storage is None:
            from services.storage_service import StorageService
            self._storage = StorageService()
        return self._storage

    def set_storage(self, storage_service):
        """设置存储服务实例（用于测试）"""
        self._storage = storage_service

    def get_file_content(self, key: str) -> Optional[bytes]:
        """获取文件内容"""
        try:
            return self.storage.get_file_content(key)
        except Exception as e:
            logger.warning(f"[CosClient] get_file_content failed for {key}: {e}")
            return None

    def put_object(self, key: str, content: bytes) -> bool:
        """上传对象"""
        try:
            self.storage.put_object(key, content)
            return True
        except Exception as e:
            logger.error(f"[CosClient] put_object failed for {key}: {e}")
            return False

    def delete_file_by_key(self, key: str) -> bool:
        """删除对象"""
        try:
            self.storage.delete_file_by_key(key)
            return True
        except Exception as e:
            logger.warning(f"[CosClient] delete_file_by_key failed for {key}: {e}")
            return False

    def list_objects(self, prefix: str) -> List[Dict]:
        """列出 COS prefix 下的对象"""
        try:
            objects = self.storage.list_objects(prefix)
            return objects if objects else []
        except Exception as e:
            logger.warning(f"[CosClient] list_objects failed for {prefix}: {e}")
            return []

    def list_objects_raw(self, bucket: str, prefix: str, max_keys: int = 1000) -> List[Dict]:
        """列出 COS 对象（通过抽象接口）"""
        try:
            objects = self.storage.list_objects(prefix, max_keys)
            return objects if objects else []
        except Exception as e:
            logger.error(f"[CosClient] list_objects_raw failed for {prefix}: {e}")
            return []
