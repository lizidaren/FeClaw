"""
Upload Session Router
POST /api/desktop/upload_session  — 创建上传会话，返回 presigned_put_url + session_id
POST /api/desktop/upload_done     — 手机直传 COS 完成后回调，触发 WS 推送
"""

import logging
from fastapi import APIRouter, Depends, HTTPException

from utils.auth import get_current_user_id
from config import settings
from services.upload_service import upload_service

router = APIRouter(prefix="/api/desktop", tags=["upload"])
logger = logging.getLogger("upload")


@router.post("/upload_session")
async def create_upload_session(user_id: int = Depends(get_current_user_id)):
    """
    创建上传会话。

    Returns:
        {
            "session_id": "abc12345",
            "presigned_put_url": "https://...",
            "expires_in": 600
        }
    """
    session_id, presigned_put_url = upload_service.create_session(user_id)
    return {
        "session_id": session_id,
        "presigned_put_url": presigned_put_url,
        "expires_in": 600,
    }


@router.post("/upload_done")
async def upload_done(request: dict, user_id: int = Depends(get_current_user_id)):
    """
    手机端直传 COS 完成后，phone/client 调用此接口。

    Request body:
        {
            "session_id": "abc12345",
            "filename": "photo.jpg"
        }

    WS push to Desktop (type=upload_complete):
        {
            "type": "upload_complete",
            "session_id": "abc12345",
            "filename": "photo.jpg",
            "presigned_get_url": "https://..."
        }
    """
    session_id = request.get("session_id")
    filename = request.get("filename", "unknown")

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    presigned_get_url = upload_service.confirm_upload(session_id, filename)
    if presigned_get_url is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    # Push to Desktop via DesktopConnectionManager
    from routers.desktop_ws import manager
    ws_push_payload = {
        "type": "upload_complete",
        "session_id": session_id,
        "filename": filename,
        "presigned_get_url": presigned_get_url,
    }
    await manager.send(ws_push_payload)
    logger.info(f"[Upload] Pushed upload_complete to Desktop: session={session_id}, filename={filename}")

    return {"status": "ok"}
