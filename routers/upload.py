"""
Upload Session Router
POST /api/desktop/upload_session  — 创建上传会话，返回 presigned_put_url + session_id
POST /api/desktop/upload_done     — 手机直传 COS 完成后回调，触发 WS 推送
"""

import logging
from fastapi import APIRouter, HTTPException

from config import settings
from services.upload_service import upload_service

router = APIRouter(prefix="/api/desktop", tags=["upload"])
logger = logging.getLogger("upload")


def _get_user_id_from_request(request) -> int:
    """Extract user_id from request state (set by auth middleware)."""
    user_id = getattr(request.state, "user_id", None)
    if user_id is None:
        # Fallback: try header for desktop clients that pass user_id differently
        raise HTTPException(status_code=401, detail="Unauthorized")
    return int(user_id)


@router.post("/upload_session")
async def create_upload_session(request: dict):
    """
    创建上传会话。

    Request body: {} (empty or with optional metadata)

    Returns:
        {
            "session_id": "abc12345",
            "presigned_put_url": "https://...",
            "expires_in": 600
        }
    """
    user_id = request.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    session_id, presigned_put_url = upload_service.create_session(int(user_id))
    return {
        "session_id": session_id,
        "presigned_put_url": presigned_put_url,
        "expires_in": 600,
    }


@router.post("/upload_done")
async def upload_done(request: dict):
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
