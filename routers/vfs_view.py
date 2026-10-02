"""
VFS 文件查看路由 - 历史图片/文件展示

GET /api/vfs/view?path=/workspace/images/...  - 查看 VFS 文件
"""
import os
import logging
from mimetypes import guess_type

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from config import settings
from models.database import get_db
from utils.auth import get_current_user_id
from utils.agent_access import user_owns_agent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["VFS View"])


@router.get("/api/vfs/view")
async def view_vfs_file(
    path: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    查看 VFS 文件（用于历史图片/文件展示）

    Q19/H13：`user_id` 此前收到但从未使用，任意登录用户可读他人
    `agents/<hash>/workspace`。现在按首段做租户隔离校验。

    Args:
        path: VFS 路径，如 /workspace/images/temp_123.png
    """
    # 路径安全检验
    sanitized = os.path.normpath(path).lstrip("/")
    if ".." in sanitized or sanitized.startswith(".."):
        raise HTTPException(status_code=400, detail="Invalid path")

    # Q19/H13：租户隔离 —— agents/<hash>/ 必须属于当前用户；user_workspaces/<uid>/ 必须是自己
    parts = [p for p in sanitized.split("/") if p]
    if parts and parts[0] == "agents" and len(parts) > 1:
        if not user_owns_agent(db, parts[1], user_id):
            raise HTTPException(status_code=403, detail="无权访问该 Agent")
    elif parts and parts[0] == "user_workspaces" and len(parts) > 1:
        if str(parts[1]) != str(user_id):
            raise HTTPException(status_code=403, detail="无权访问该用户空间")

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
