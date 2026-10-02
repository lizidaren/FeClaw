"""
FileStorage 抽象层 — 分离文件存储后端

用法:
    from services.file_storage import create_file_storage
    storage = create_file_storage()           # auto: 有 COS 配置则用 COS，否则本地
    storage = create_file_storage(mode="cos") # 强制 COS
    storage = create_file_storage(mode="local") # 强制本地磁盘
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class FileStorage(ABC):
    """文件存储抽象基类"""

    @abstractmethod
    def get_file_content(self, key: str) -> Optional[bytes]:
        """获取文件内容

        Args:
            key: 存储路径

        Returns:
            文件字节数据，文件不存在时返回 None
        """
        ...

    @abstractmethod
    def put_object(self, key: str, file_bytes: bytes) -> None:
        """写入文件

        Args:
            key: 存储路径
            file_bytes: 文件字节数据
        """
        ...

    @abstractmethod
    def delete_file_by_key(self, key: str) -> bool:
        """删除文件

        Returns:
            True 删除成功 / False 文件不存在
        """
        ...

    @abstractmethod
    def list_objects(self, prefix: str, max_keys: int = 1000) -> Optional[List[Dict]]:
        """列出前缀下的所有对象

        Returns:
            对象列表，每个对象含 Key, Size, LastModified 字段
            失败时返回 None
        """
        ...

    @abstractmethod
    def file_exists(self, key: str) -> Optional[Dict]:
        """检查文件是否存在并返回元数据（不下载内容）

        对标 COS head_object 语义。

        Args:
            key: 存储路径

        Returns:
            文件元数据 dict（含 size, mtime 等），不存在时返回 None
        """
        ...

    # ==================== 异步包装方法（所有后端通用） ====================
    # 之前只有 CosStorage 提供 *_async 方法，LocalStorage 没有；工具层（如
    # file_ops.py 的群共享空间 file_read/file_write）通过 self.storage 调用这些方法，
    # 一旦 self.storage 按 STORAGE_MODE 切到 LocalStorage 就会 AttributeError。
    # 这里统一在基类提供，LocalStorage / CosStorage 均可用。

    async def put_object_async(self, key: str, file_bytes: bytes) -> None:
        """异步包装：写入文件"""
        return await asyncio.to_thread(self.put_object, key, file_bytes)

    async def get_file_content_async(self, key: str) -> Optional[bytes]:
        """异步包装：获取文件内容"""
        return await asyncio.to_thread(self.get_file_content, key)

    async def list_objects_async(self, prefix: str, max_keys: int = 1000) -> Optional[List[Dict]]:
        """异步包装：列出对象"""
        return await asyncio.to_thread(self.list_objects, prefix, max_keys)

    async def delete_file_by_key_async(self, key: str) -> bool:
        """异步包装：按 key 删除文件"""
        return await asyncio.to_thread(self.delete_file_by_key, key)


def create_file_storage(mode: str = "auto") -> FileStorage:
    """自动选择存储后端

    Args:
        mode: "auto" | "cos" | "local"
            auto: 有 COS 配置则用 COS，否则本地
            cos:  强制 COS（COS 配置不完整时抛异常）
            local: 强制本地磁盘
    """
    from config import settings

    if mode == "local":
        from services.local_storage import LocalStorage
        root = getattr(settings, "LOCAL_STORAGE_ROOT", "./feclaw-storage")
        pub_root = getattr(settings, "PUBLIC_STORAGE_ROOT", "./feclaw-public")
        return LocalStorage(root_dir=root, public_root=pub_root)

    cos_configured = all([
        settings.TENCENT_COS_SECRET_ID,
        settings.TENCENT_COS_SECRET_KEY,
        settings.TENCENT_COS_BUCKET,
    ])

    if mode == "cos" and not cos_configured:
        raise ValueError("COS mode requires TENCENT_COS_* config")

    if cos_configured:
        from services.storage_service import CosStorage
        return CosStorage()

    if mode == "auto":
        logger.info("COS not configured, falling back to LocalStorage")
        from services.local_storage import LocalStorage
        root = getattr(settings, "LOCAL_STORAGE_ROOT", "./feclaw-storage")
        pub_root = getattr(settings, "PUBLIC_STORAGE_ROOT", "./feclaw-public")
        return LocalStorage(root_dir=root, public_root=pub_root)

    raise ValueError(f"Unknown storage mode: {mode}")
