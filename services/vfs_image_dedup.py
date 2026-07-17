"""
VFS Image Deduplication Service - 按 hash+大小避免重复存储图片

功能：
- 检测重复图片（基于 SHA256 hash + 文件大小）
- 复用已存在的图片，不重复上传
- 维护图片 hash 清单（存储在 COS）

原理：
- 用户上传图片时，先计算 hash+size
- 查询 manifest 中是否存在相同 hash+size 的记录
- 如果存在，返回已存储的 VFS 路径；如果不存在，上传并记录

用途：
- VFS cp/mv/echo 操作时检查是否重复
- workspace_service 上传图片时去重
"""

import json
import hashlib
import logging
from dataclasses import dataclass
from typing import Optional, Tuple, Dict
from datetime import datetime, timezone

from config import settings
from services.file_storage import create_file_storage

logger = logging.getLogger(__name__)


@dataclass
class ImageRecord:
    """图片记录"""
    vfs_path: str         # VFS 路径（如 /workspace/images/xxx.png）
    cos_key: str          # COS 存储 key
    size: int             # 文件大小（字节）
    sha256_hash: str      # SHA256 内容哈希
    uploaded_at: str      # 上传时间（ISO 格式）
    original_filename: str  # 原始文件名（如果有）


class VFSImageDeduplicationService:
    """
    VFS 图片去重服务
    
    用法：

    ```python
    from services.vfs_image_dedup import VFSImageDeduplicationService

    dedup = VFSImageDeduplicationService(agent_hash="abcd1234")

    # 检查图片是否重复（返回已存在的 VFS 路径或 None）
    existing_path = dedup.find_duplicate(image_bytes)  # 注意：参数为 bytes，不是 BytesIO

    # 注册新图片到去重清单
    dedup.register_image("/workspace/images/photo.png", image_bytes, original_filename="photo.jpg")

    # 批量检查（cp/mv 时用）
    is_dup, existing = dedup.check_duplicate_for_path("/workspace/images/photo.png", image_bytes)
    ```
    """

    # Manifest 存储路径前缀
    MANIFEST_PREFIX = ".vfs_images_dedup"
    MANIFEST_NAME = "_manifest.json"

    def __init__(self, user_id: str = None, agent_hash: str = None, storage_service=None):
        self.user_id = str(user_id) if user_id else None
        self.agent_hash = agent_hash
        self._storage = storage_service
        self._manifest_cache: Optional[Dict] = None

    @property
    def _id(self) -> str:
        """返回用于路径构建的标识符（优先 agent_hash，回退 user_id）"""
        return self.agent_hash or self.user_id or "unknown"

    @property
    def storage(self):
        if self._storage is None:
            self._storage = create_file_storage()
        return self._storage

    def _manifest_key(self) -> str:
        """获取 manifest 文件的 COS key"""
        return f"{self.MANIFEST_PREFIX}/{self.agent_hash or self.user_id}/{self.MANIFEST_NAME}"

    def get_manifest(self) -> Dict:
        """Public accessor for the dedup manifest."""
        return self._get_manifest()

    def _get_manifest(self) -> Dict:
        """
        获取图片去重清单（从 COS 加载或创建空清单）
        
        Manifest 结构：
        {
            "images": {
                "{sha256_hash}_{size}": {
                    "vfs_path": "/workspace/images/xxx.png",
                    "cos_key": "firstentrance/mistakes/user_123/workspace/images/xxx.png",
                    "size": 12345,
                    "sha256_hash": "abc123...",
                    "uploaded_at": "2026-04-20T03:30:00",
                    "original_filename": "photo.jpg"
                }
            }
        }
        """
        if self._manifest_cache is not None:
            return self._manifest_cache

        manifest_key = self._manifest_key()
        content = self.storage.get_file_content(manifest_key)

        if content:
            try:
                self._manifest_cache = json.loads(content.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._manifest_cache = {"images": {}}
        else:
            self._manifest_cache = {"images": {}}

        return self._manifest_cache

    def _save_manifest(self, manifest: Dict) -> bool:
        """保存 manifest 到 COS"""
        try:
            manifest_key = self._manifest_key()
            content = json.dumps(manifest, ensure_ascii=False, indent=2)
            self.storage.put_object(manifest_key, content.encode("utf-8"))
            self._manifest_cache = manifest
            return True
        except Exception as e:
            logger.error(f"[VFSImageDedup] 保存 manifest 失败: {e}")
            return False

    def _vfs_path_to_cos_key(self, vfs_path: str) -> str:
        """
        将 VFS 路径转换为 COS key

        Args:
            vfs_path: VFS 路径（如 /workspace/images/photo.png）

        Returns:
            COS key（如 feclaw/agents/{hash}/workspace/images/photo.png）
        """
        # 去掉前导 /
        normalized_path = vfs_path.lstrip("/")

        if self.agent_hash:
            cos_key = f"{settings.TENCENT_COS_PREFIX}agents/{self.agent_hash}/{normalized_path}"
        else:
            cos_key = f"{settings.TENCENT_COS_PREFIX}{self.user_id}/{normalized_path}"

        return cos_key

    def _compute_hash_and_size(self, data: bytes) -> Tuple[str, int]:
        """计算图片的 SHA256 hash 和大小"""
        hash_sha256 = hashlib.sha256(data).hexdigest()
        return hash_sha256, len(data)

    def _make_key(self, sha256_hash: str, size: int) -> str:
        """生成去重查找的 key"""
        return f"{sha256_hash}_{size}"

    def find_duplicate(self, image_data: bytes) -> Optional[str]:
        """
        检查图片数据是否已存在

        Args:
            image_data: 图片字节数据

        Returns:
            已存在的 VFS 路径，如果不存在返回 None
        """
        sha256_hash, size = self._compute_hash_and_size(image_data)
        dedup_key = self._make_key(sha256_hash, size)

        manifest = self._get_manifest()
        image_info = manifest.get("images", {}).get(dedup_key)

        if image_info:
            logger.info(f"[VFSImageDedup] 找到重复图片: {image_info['vfs_path']} (hash={sha256_hash[:16]}..., size={size})")
            return image_info["vfs_path"]

        return None

    def register_image(self, vfs_path: str, image_data: bytes, original_filename: str = "") -> bool:
        """
        将新图片注册到去重清单

        Args:
            vfs_path: VFS 路径（如 /workspace/images/photo.png）
            image_data: 图片字节数据
            original_filename: 原始文件名（可选）

        Returns:
            是否注册成功
        """
        # 如果图片已存在，不重复注册
        existing = self.find_duplicate(image_data)
        if existing and existing != vfs_path:
            logger.info(f"[VFSImageDedup] 图片已存在，跳过注册: {existing}")
            return False

        sha256_hash, size = self._compute_hash_and_size(image_data)
        dedup_key = self._make_key(sha256_hash, size)

        # 计算 COS key（用于后续清理验证）
        cos_key = self._vfs_path_to_cos_key(vfs_path)

        manifest = self._get_manifest()
        manifest["images"][dedup_key] = {
            "vfs_path": vfs_path,
            "cos_key": cos_key,
            "size": size,
            "sha256_hash": sha256_hash,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "original_filename": original_filename
        }

        return self._save_manifest(manifest)

    def check_duplicate_for_path(self, vfs_path: str, image_data: bytes) -> Tuple[bool, Optional[str]]:
        """
        检查并返回结果（用于 cp/mv 操作前的决策）

        Args:
            vfs_path: 目标 VFS 路径
            image_data: 图片字节数据

        Returns:
            (is_duplicate, existing_vfs_path)
        """
        existing_path = self.find_duplicate(image_data)
        if existing_path:
            return True, existing_path
        return False, None

    def get_image_info(self, sha256_hash: str, size: int) -> Optional[Dict]:
        """
        根据 hash+size 获取图片记录信息

        Args:
            sha256_hash: SHA256 哈希值
            size: 文件大小

        Returns:
            图片记录 dict，如果不存在返回 None
        """
        dedup_key = self._make_key(sha256_hash, size)
        manifest = self._get_manifest()
        return manifest.get("images", {}).get(dedup_key)

    def stats(self) -> Dict:
        """
        获取去重统计信息

        Returns:
            {"total_images": N, "total_size": bytes, "dedup_count": M}
        """
        manifest = self._get_manifest()
        images = manifest.get("images", {})

        total_size = sum(img.get("size", 0) for img in images.values())

        return {
            "total_images": len(images),
            "total_size": total_size,
            "manifest_key": self._manifest_key()
        }
    
    def detailed_stats(self) -> Dict:
        """
        获取详细统计信息
        
        包括：
        - 按文件类型的分布
        - 平均文件大小
        - 最近上传时间
        - 去重效果估算
        
        Returns:
            {
                "total_images": int,
                "total_size": int,
                "avg_size": int,
                "type_distribution": {"png": N, "jpg": M, ...},
                "latest_upload": str (ISO datetime),
                "size_range": {"min": int, "max": int},
                "manifest_key": str
            }
        """
        manifest = self._get_manifest()
        images = manifest.get("images", {})
        
        if not images:
            return {
                "total_images": 0,
                "total_size": 0,
                "avg_size": 0,
                "type_distribution": {},
                "latest_upload": None,
                "size_range": {"min": 0, "max": 0},
                "manifest_key": self._manifest_key()
            }
        
        total_size = 0
        type_counts = {}
        upload_times = []
        sizes = []
        
        for img_info in images.values():
            size = img_info.get("size", 0)
            total_size += size
            sizes.append(size)
            
            # 统计文件类型
            vfs_path = img_info.get("vfs_path", "")
            ext = self._extract_extension(vfs_path)
            if ext:
                type_counts[ext] = type_counts.get(ext, 0) + 1
            
            # 收集上传时间
            uploaded_at = img_info.get("uploaded_at")
            if uploaded_at:
                upload_times.append(uploaded_at)
        
        # 计算平均大小
        avg_size = total_size / len(images) if images else 0
        
        # 找出最近上传时间
        latest_upload = max(upload_times) if upload_times else None
        
        # 计算大小范围
        size_range = {
            "min": min(sizes) if sizes else 0,
            "max": max(sizes) if sizes else 0
        }
        
        return {
            "total_images": len(images),
            "total_size": total_size,
            "avg_size": int(avg_size),
            "type_distribution": type_counts,
            "latest_upload": latest_upload,
            "size_range": size_range,
            "manifest_key": self._manifest_key()
        }
    
    def _extract_extension(self, vfs_path: str) -> str:
        """
        从 VFS 路径提取文件扩展名
        
        Args:
            vfs_path: VFS 路径
        
        Returns:
            文件扩展名（如 "png", "jpg"，不带点）
        """
        if not vfs_path:
            return "unknown"
        
        # 去掉前导 / 和查询参数
        path = vfs_path.split("?")[0].lstrip("/")
        
        # 提取扩展名
        if "." in path:
            ext = path.rsplit(".", 1)[-1].lower()
            # 只保留常见的图片扩展名
            valid_exts = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "ico"}
            if ext in valid_exts:
                return ext
        
        return "unknown"

    def unregister_image(self, vfs_path: str) -> bool:
        """
        从去重清单中移除图片记录
        
        当用户通过 VFS 删除图片时，应调用此方法同步清理 manifest。
        
        Args:
            vfs_path: VFS 路径（如 /workspace/images/photo.png）
        
        Returns:
            是否成功移除（False 表示记录不存在）
        """
        manifest = self._get_manifest()
        images = manifest.get("images", {})
        
        # 查找对应的记录（可能有多条，但正常情况应该只有一条）
        keys_to_remove = []
        for dedup_key, img_info in images.items():
            if img_info.get("vfs_path") == vfs_path:
                keys_to_remove.append(dedup_key)
        
        if not keys_to_remove:
            logger.warning(f"[VFSImageDedup] 未找到要移除的记录: {vfs_path}")
            return False
        
        for key in keys_to_remove:
            del manifest["images"][key]
        
        self._save_manifest(manifest)
        logger.info(f"[VFSImageDedup] 已移除 {len(keys_to_remove)} 条记录: {vfs_path}")
        return True

    def batch_check_duplicates(self, images: list) -> Dict[str, Optional[str]]:
        """
        批量检查图片是否重复
        
        Args:
            images: 图片列表，每项为 {"data": bytes, "vfs_path": str} 或 (data, vfs_path) 元组
        
        Returns:
            {vfs_path: existing_path_or_None}
        
        Example:
            results = dedup.batch_check_duplicates([
                {"data": img1_bytes, "vfs_path": "/img1.png"},
                {"data": img2_bytes, "vfs_path": "/img2.png"},
            ])
            # results = {"/img1.png": None, "/img2.png": "/existing.png"}
        """
        results = {}
        
        for item in images:
            # 支持字典和元组两种格式
            if isinstance(item, dict):
                data = item.get("data")
                vfs_path = item.get("vfs_path", "unknown")
            else:
                data, vfs_path = item[0], item[1] if len(item) > 1 else "unknown"
            
            if data:
                existing = self.find_duplicate(data)
                results[vfs_path] = existing
        
        return results

    def cleanup_stale_records(self, dry_run: bool = False) -> Dict:
        """
        清理指向不存在文件的记录
        
        当用户删除图片文件后，manifest 中的记录可能残留。
        此方法检查每条记录对应的文件是否仍在 COS 中存在，
        如果不存在，则清理该记录。
        
        Args:
            dry_run: 只预览，不实际删除
        
        Returns:
            {
                "removed_count": N, 
                "remaining_count": M, 
                "removed_records": [{"vfs_path": "...", "reason": "..."}],
                "dry_run": bool
            }
        """
        manifest = self._get_manifest()
        images = manifest.get("images", {})
        
        removed_records = []
        keys_to_remove = []
        
        for dedup_key, img_info in images.items():
            cos_key = img_info.get("cos_key", "")
            vfs_path = img_info.get("vfs_path", "unknown")
            
            # 如果 cos_key 为空，尝试从 vfs_path 计算
            if not cos_key:
                cos_key = self._vfs_path_to_cos_key(vfs_path)
            
            # 检查文件是否存在
            file_content = self.storage.get_file_content(cos_key)
            if file_content is None:
                # 文件不存在，标记为需要清理
                removed_records.append({
                    "vfs_path": vfs_path,
                    "cos_key": cos_key,
                    "reason": "file_not_found",
                    "size": img_info.get("size", 0)
                })
                keys_to_remove.append(dedup_key)
        
        # 如果不是 dry_run，实际删除记录
        if not dry_run and keys_to_remove:
            for key in keys_to_remove:
                del manifest["images"][key]
            self._save_manifest(manifest)
        
        remaining_count = len(images) - len(keys_to_remove)
        
        return {
            "removed_count": len(keys_to_remove),
            "remaining_count": remaining_count,
            "removed_records": removed_records,
            "dry_run": dry_run
        }