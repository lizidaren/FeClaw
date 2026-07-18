"""
通用文件上传路由 (P0-1 fix)
POST /api/upload  - multipart/form-data 上传文件 → COS / 本地存储

Mobile 前端需要：上传任意文件后返回可直接访问的 {url, mime, size, name}。
参考 zentrim.upload_attachment 但不依赖 entry_id。
"""

import logging
import os
import re
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from config import settings
from models.database import get_db
from services.file_storage import create_file_storage
from utils.auth import get_current_user_id

logger = logging.getLogger("upload_general")

router = APIRouter(prefix="/api", tags=["upload"])

# 文件大小限制 50MB
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# 文件名安全白名单：去除路径分隔符、控制字符等
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(name: str) -> str:
    """清理文件名：去掉路径分隔符 + 限制长度 + 兜底默认值"""
    if not name:
        return "file"
    # 去掉目录部分
    base = os.path.basename(name.replace("\\", "/"))
    # 替换非法字符
    base = _FILENAME_SAFE.sub("_", base).strip("._")
    if not base:
        base = "file"
    # 限制单段长度
    if len(base) > 200:
        stem, dot, ext = base.rpartition(".")
        if dot:
            base = stem[:180] + "." + ext[:16]
        else:
            base = base[:200]
    return base


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    通用文件上传

    - 接收 multipart/form-data，字段名 `file`
    - 50MB 大小上限
    - COS path: `{prefix}uploads/{user_id}/{uuid}_{filename}`
    - COS 未配置时回落到 `storage/uploads/{user_id}/...`
    - 返回 `{status, url, mime, size, name}`
    """
    try:
        file_bytes = await file.read()
    finally:
        await file.close()

    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (> {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)",
        )

    safe_name = _sanitize_filename(file.filename or "file")
    mime = file.content_type or "application/octet-stream"
    unique_id = uuid.uuid4().hex[:16]
    object_name = f"{unique_id}_{safe_name}"

    # 优先使用 COS（auto 模式下 create_file_storage 已自动选择）
    storage = create_file_storage(mode="auto")

    cos_prefix = settings.TENCENT_COS_PREFIX or "feclaw/"
    if not cos_prefix.endswith("/"):
        cos_prefix = cos_prefix + "/"
    cos_key = f"{cos_prefix}uploads/{user_id}/{object_name}"

    try:
        storage.put_object(cos_key, file_bytes)
    except Exception as e:
        logger.error(f"[upload] COS put_object failed, fallback to local: {e}")
        # COS 写入失败时回落到本地存储
        local_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "storage",
            "uploads",
            str(user_id),
        )
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, object_name)
        with open(local_path, "wb") as f:
            f.write(file_bytes)
        # 本地服务通过 /static/uploads/... 暴露（main.py 已挂载 /static 目录）
        # 这里直接返回 web 可访问的 URL
        url = f"/static/uploads/{user_id}/{object_name}"
        logger.info(
            f"[upload] saved locally (fallback) user={user_id} name={safe_name} size={len(file_bytes)}"
        )
        return {
            "status": "ok",
            "url": url,
            "mime": mime,
            "size": len(file_bytes),
            "name": safe_name,
        }

    # COS URL（COS 公开读需要 bucket 配置公开读权限；否则需走 presigned get）
    if settings.TENCENT_COS_BUCKET and settings.TENCENT_COS_REGION:
        url = f"https://{settings.TENCENT_COS_BUCKET}.cos.{settings.TENCENT_COS_REGION}.myqcloud.com/{cos_key}"
    else:
        # 没有完整 COS 配置但 put_object 没抛异常 —— 仍走本地公开 URL
        url = f"/static/uploads/{user_id}/{object_name}"

    logger.info(
        f"[upload] user={user_id} name={safe_name} size={len(file_bytes)} "
        f"key={cos_key} storage={'cos' if storage.__class__.__name__ == 'CosStorage' else 'local'}"
    )

    return {
        "status": "ok",
        "url": url,
        "mime": mime,
        "size": len(file_bytes),
        "name": safe_name,
    }
