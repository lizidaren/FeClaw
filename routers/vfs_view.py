"""
VFS 文件查看路由 - 历史图片/文件展示

GET /api/vfs/view?path=/workspace/images/...  - 查看 VFS 文件
"""
import os
import logging
from mimetypes import guess_type

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from config import settings
from utils.auth import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["VFS View"])


@router.get("/api/vfs/view")
async def view_vfs_file(
    path: str,
    user_id: int = Depends(get_current_user_id),
):
    """
    查看 VFS 文件（用于历史图片/文件展示）

    Args:
        path: VFS 路径，如 /workspace/images/temp_123.png
    """
    # 路径安全检验
    sanitized = os.path.normpath(path).lstrip("/")
    if ".." in sanitized or sanitized.startswith(".."):
        raise HTTPException(status_code=400, detail="Invalid path")

    # 解析到 FUSE 实际文件系统路径
    fuse_root = os.path.realpath(settings.FUSE_MOUNT_DIR)
    full_path = os.path.realpath(os.path.join(fuse_root, sanitized))

    # 防止路径穿越
    if not full_path.startswith(fuse_root + os.sep) and full_path != fuse_root:
        raise HTTPException(status_code=403, detail="Path traversal denied")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")

    mime_type = guess_type(path)[0] or "application/octet-stream"
    return FileResponse(full_path, media_type=mime_type)
