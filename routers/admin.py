"""管理后台 API 路由"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status

from models.database import User
from utils.auth import get_current_user, get_current_user_optional
from services.active_tracker import get_active, get_recent
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["管理后台"])


@router.get("/requests")
async def get_active_requests(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """获取活跃请求信息（API key 或管理员）"""
    # API key 认证（Platform 跨服调用）
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
        if settings.ADMIN_API_KEY and token == settings.ADMIN_API_KEY:
            pass  # API key 通过
        elif not current_user or not current_user.is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    elif not current_user or not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")

    active = get_active()
    recent = get_recent()

    return {
        "active_count": len(active),
        "active": active,
        "recent_30m": list(recent.values()),
    }
