"""
FileStorage 抽象层 — 分离文件存储后端

用法:
    from services.file_storage import create_file_storage
    storage = create_file_storage()           # auto: 有 COS 配置则用 COS，否则本地
    storage = create_file_storage(mode="cos") # 强制 COS
    storage = create_file_storage(mode="local") # 强制本地磁盘
"""

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
        return LocalStorage(root_dir=root)

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
        return LocalStorage(root_dir=root)

    raise ValueError(f"Unknown storage mode: {mode}")
