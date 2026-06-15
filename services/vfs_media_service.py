"""
VFS Media Service - 附件存储服务（含 SHA256 哈希去重）

功能：
- 保存附件（图片/文件）到 VFS，自动去重
- SHA256 哈希去重，复用已存在的文件
- 保护去重目录不被 Agent 工具修改

用法：
    from services.vfs_media_service import VFSMediaService

    media = VFSMediaService()
    vfs_path = await media.save_attachment(agent_hash, raw_data, "image/png")
    # vfs_path: vfs:///uploads/dedup/abc123.png
"""

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from config import settings
from services.storage_service import get_storage_service
from services.vfs_image_dedup import VFSImageDeduplicationService

logger = logging.getLogger(__name__)

# 支持的 MIME 类型 → 文件扩展名映射
MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/markdown": ".md",
    "application/json": ".json",
    "application/zip": ".zip",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


class VFSMediaService:
    """附件存储服务（含 SHA256 哈希去重）

    在 VFSImageDeduplicationService 基础上封装，提供通用的附件保存接口。
    去重目录 /uploads/dedup/ 受保护，Agent 工具不可直接操作。
    """

    DEDUP_DIR = "/uploads/dedup/"

    def __init__(self, storage_service=None):
        self._storage = storage_service

    @property
    def storage(self):
        if self._storage is None:
            self._storage = get_storage_service()
        return self._storage

    @staticmethod
    def _compute_sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _ext_from_mime(mime_type: str) -> str:
        """从 MIME 类型推断文件扩展名"""
        mime_type = (mime_type or "").lower().strip()
        ext = MIME_TO_EXT.get(mime_type)
        if ext:
            return ext
        # 尝试从 MIME 子类型推断
        if "/" in mime_type:
            subtype = mime_type.split("/")[-1]
            return f".{subtype}" if subtype else ".bin"
        return ".bin"

    def _cos_key(self, agent_hash: str, relative_path: str) -> str:
        """构建 COS 存储 key"""
        normalized = relative_path.lstrip("/")
        return f"{settings.TENCENT_COS_PREFIX}agents/{agent_hash}/{normalized}"

    def is_protected_path(self, path: str) -> bool:
        """去重目录标记为只读，Agent 工具不可操作"""
        return path.startswith(self.DEDUP_DIR)

    async def save_attachment(
        self,
        agent_hash: str,
        raw_data: bytes,
        mime_type: str = "application/octet-stream",
        original_filename: str = "",
    ) -> str:
        """保存附件到 VFS，返回 vfs:// 路径（SHA256 哈希去重）

        Args:
            agent_hash: Agent 标识
            raw_data: 附件原始字节数据
            mime_type: MIME 类型（如 "image/png"）
            original_filename: 原始文件名（可选）

        Returns:
            VFS 路径，格式如 vfs:///uploads/dedup/{hash}.{ext}
        """
        if not raw_data:
            raise ValueError("raw_data must not be empty")

        file_hash = self._compute_sha256(raw_data)
        ext = self._ext_from_mime(mime_type)

        # 查询去重：是否已有相同内容的文件
        dedup = VFSImageDeduplicationService(agent_hash=agent_hash)
        existing = dedup.find_duplicate(raw_data)
        if existing:
            logger.info(
                f"[VFSMedia] 去重命中: hash={file_hash[:16]}... -> {existing}"
            )
            return existing

        # 新文件：上传到 COS
        relative_path = f"uploads/dedup/{file_hash}{ext}"
        cos_key = self._cos_key(agent_hash, relative_path)

        self.storage.put_object(cos_key, raw_data)

        # 注册到去重清单
        vfs_path = f"{self.DEDUP_DIR}{file_hash}{ext}"
        dedup.register_image(vfs_path, raw_data, original_filename=original_filename)

        logger.info(
            f"[VFSMedia] 已保存: {vfs_path} (hash={file_hash[:16]}..., "
            f"size={len(raw_data)}, mime={mime_type})"
        )
        return vfs_path

    async def save_attachment_batch(
        self,
        agent_hash: str,
        attachments: list,
    ) -> list:
        """批量保存附件

        Args:
            agent_hash: Agent 标识
            attachments: [{"raw_data": bytes, "mime_type": str, "filename": str}, ...]

        Returns:
            [vfs_path, ...]
        """
        results = []
        for att in attachments:
            path = await self.save_attachment(
                agent_hash=agent_hash,
                raw_data=att["raw_data"],
                mime_type=att.get("mime_type", "application/octet-stream"),
                original_filename=att.get("filename", ""),
            )
            results.append(path)
        return results
