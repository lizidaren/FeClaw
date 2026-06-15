"""
VFS 图片去重管理 API 路由

提供图片去重清单的管理接口：
- GET /api/vfs-images/stats - 获取去重统计
- GET /api/vfs-images/stats/detailed - 获取详细统计
- POST /api/vfs-images/cleanup - 清理过期记录
- GET /api/vfs-images/list - 获取去重清单列表（可选）
"""

from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from services.vfs_image_dedup import VFSImageDeduplicationService
from services.storage_service import get_storage_service
from utils.auth import get_current_user


router = APIRouter(prefix="/api/vfs-images", tags=["VFS 图片去重"])


# ==================== Pydantic 模型 ====================

class StatsResponse(BaseModel):
    """统计响应"""
    total_images: int = Field(..., description="去重清单中的图片总数")
    total_size: int = Field(..., description="图片总大小（字节）")
    manifest_key: str = Field(..., description="清单文件存储路径")


class DetailedStatsResponse(BaseModel):
    """详细统计响应"""
    total_images: int = Field(..., description="图片总数")
    total_size: int = Field(..., description="总大小（字节）")
    avg_size: int = Field(..., description="平均大小（字节）")
    type_distribution: Dict[str, int] = Field(..., description="按文件类型分布")
    latest_upload: Optional[str] = Field(None, description="最近上传时间（ISO 格式）")
    size_range: Dict[str, int] = Field(..., description="大小范围（min/max）")
    manifest_key: str = Field(..., description="清单文件存储路径")


class CleanupRequest(BaseModel):
    """清理请求"""
    dry_run: bool = Field(False, description="只预览，不实际删除")


class CleanupResponse(BaseModel):
    """清理响应"""
    removed_count: int = Field(..., description="清理的记录数")
    remaining_count: int = Field(..., description="剩余记录数")
    removed_records: List[Dict[str, Any]] = Field(..., description="被清理的记录详情")
    dry_run: bool = Field(..., description="是否为预览模式")


class ImageListItem(BaseModel):
    """图片清单项"""
    vfs_path: str = Field(..., description="VFS 路径")
    size: int = Field(..., description="文件大小")
    sha256_hash: str = Field(..., description="SHA256 哈希值")
    uploaded_at: str = Field(..., description="上传时间")
    original_filename: Optional[str] = Field(None, description="原始文件名")


class ManifestListResponse(BaseModel):
    """清单列表响应"""
    total_images: int = Field(..., description="图片总数")
    images: List[ImageListItem] = Field(..., description="图片列表")


# ==================== 服务实例 ====================

def get_image_dedup_service(
    user_id: int = Depends(get_current_user),
    storage = Depends(get_storage_service)
) -> VFSImageDeduplicationService:
    """获取 VFSImageDeduplicationService 实例"""
    return VFSImageDeduplicationService(
        user_id=str(user_id),
        storage_service=storage
    )


# ==================== 统计 API ====================

@router.get("/stats", response_model=StatsResponse, summary="获取去重统计")
async def get_stats(
    dedup: VFSImageDeduplicationService = Depends(get_image_dedup_service)
):
    """
    获取用户的图片去重统计信息
    
    返回：
    - total_images: 去重清单中的图片总数
    - total_size: 图片总大小（字节）
    - manifest_key: 清单文件存储路径
    """
    stats = dedup.stats()
    
    return StatsResponse(
        total_images=stats["total_images"],
        total_size=stats["total_size"],
        manifest_key=stats["manifest_key"]
    )


@router.get("/stats/detailed", response_model=DetailedStatsResponse, summary="获取详细统计")
async def get_detailed_stats(
    dedup: VFSImageDeduplicationService = Depends(get_image_dedup_service)
):
    """
    获取用户的图片去重详细统计
    
    包括：
    - 图片总数、总大小、平均大小
    - 按文件类型分布（png, jpg, jpeg, gif, webp, bmp 等）
    - 最近上传时间
    - 大小范围（最小/最大）
    """
    stats = dedup.detailed_stats()
    
    return DetailedStatsResponse(
        total_images=stats["total_images"],
        total_size=stats["total_size"],
        avg_size=stats["avg_size"],
        type_distribution=stats["type_distribution"],
        latest_upload=stats["latest_upload"],
        size_range=stats["size_range"],
        manifest_key=stats["manifest_key"]
    )


# ==================== 清理 API ====================

@router.post("/cleanup", response_model=CleanupResponse, summary="清理过期记录")
async def cleanup_stale_records(
    request: CleanupRequest,
    dedup: VFSImageDeduplicationService = Depends(get_image_dedup_service)
):
    """
    清理指向不存在文件的过期记录
    
    当用户删除图片文件后，去重清单中的记录可能残留。
    此 API 检查每条记录对应的文件是否仍在 COS 中存在，
    如果不存在，则清理该记录。
    
    参数：
    - dry_run: true 只预览不删除，false 实际删除
    
    返回：
    - removed_count: 清理的记录数
    - remaining_count: 剩余记录数
    - removed_records: 被清理的记录详情（vfs_path, cos_key, reason, size）
    """
    result = dedup.cleanup_stale_records(dry_run=request.dry_run)
    
    return CleanupResponse(
        removed_count=result["removed_count"],
        remaining_count=result["remaining_count"],
        removed_records=result["removed_records"],
        dry_run=result["dry_run"]
    )


# ==================== 清单管理 API ====================

@router.get("/list", response_model=ManifestListResponse, summary="获取去重清单列表")
async def get_manifest_list(
    limit: int = Query(100, ge=1, le=1000, description="返回数量限制"),
    offset: int = Query(0, ge=0, description="偏移量"),
    dedup: VFSImageDeduplicationService = Depends(get_image_dedup_service)
):
    """
    获取用户的图片去重清单列表
    
    参数：
    - limit: 返回数量限制（1-1000）
    - offset: 偏移量（分页）
    
    返回：
    - total_images: 图片总数
    - images: 图片列表（vfs_path, size, sha256_hash, uploaded_at, original_filename）
    """
    manifest = dedup.get_manifest()
    images_data = manifest.get("images", {})
    
    # 转换为列表格式
    all_images = []
    for dedup_key, img_info in images_data.items():
        all_images.append(ImageListItem(
            vfs_path=img_info.get("vfs_path", ""),
            size=img_info.get("size", 0),
            sha256_hash=img_info.get("sha256_hash", ""),
            uploaded_at=img_info.get("uploaded_at", ""),
            original_filename=img_info.get("original_filename")
        ))
    
    # 分页
    paginated_images = all_images[offset:offset + limit]
    
    return ManifestListResponse(
        total_images=len(all_images),
        images=paginated_images
    )


@router.delete("/record", summary="删除指定图片记录")
async def delete_image_record(
    vfs_path: str = Query(..., description="VFS 路径"),
    dedup: VFSImageDeduplicationService = Depends(get_image_dedup_service)
) -> dict:
    """
    从去重清单中删除指定图片记录
    
    参数：
    - vfs_path: VFS 路径（如 /workspace/images/photo.png）
    
    返回：
    - success: 是否成功删除
    - message: 操作结果说明
    """
    result = dedup.unregister_image(vfs_path)
    
    if result:
        return {"success": True, "message": f"已移除记录: {vfs_path}"}
    else:
        raise HTTPException(status_code=404, detail=f"未找到记录: {vfs_path}")