"""管理后台 API 路由"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func

from models.database import User, ChatHistory, ConversationSession, AgentProfile, SessionLocal
from utils.auth import get_current_user, get_current_user_optional
from services.active_tracker import get_active, get_recent
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["管理后台"])


def _check_auth(request: Request, current_user: Optional[User]):
    """检查 API key 或管理员权限"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
        if settings.ADMIN_API_KEY and token == settings.ADMIN_API_KEY:
            return True
    if current_user and current_user.is_admin:
        return True
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")


@router.get("/requests")
async def get_active_requests(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """获取活跃请求信息"""
    _check_auth(request, current_user)
    active = get_active()
    recent = get_recent()
    return {
        "active_count": len(active),
        "active": active,
        "recent_30m": list(recent.values()),
    }


@router.get("/stats")
async def get_stats(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """获取综合统计信息"""
    _check_auth(request, current_user)

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)

        # 用户统计
        total_users = db.query(User).count()
        users_today = db.query(User).filter(User.created_at >= today_start).count()
        users_7d = db.query(User).filter(User.created_at >= week_ago).count()

        # Agent 统计
        total_agents = db.query(AgentProfile).count()
        active_agents_7d = db.query(AgentProfile).filter(
            AgentProfile.updated_at >= week_ago
        ).count()

        # 消息统计（今天）
        messages_today = db.query(ChatHistory).filter(
            ChatHistory.created_at >= today_start
        ).count()
        total_messages = db.query(ChatHistory).count()

        # 会话统计
        total_sessions = db.query(ConversationSession).count()
        active_sessions_7d = db.query(ConversationSession).filter(
            ConversationSession.updated_at >= week_ago
        ).count()

        # 消息按渠道分布（今日）
        channel_stats = db.query(
            ChatHistory.channel,
            func.count(ChatHistory.id).label("count")
        ).filter(
            ChatHistory.created_at >= today_start,
            ChatHistory.channel.isnot(None)
        ).group_by(ChatHistory.channel).all()

        # 最活跃 Agent（今日）
        active_today = get_active()
        recent_activity = get_recent()

        return {
            "users": {
                "total": total_users,
                "today": users_today,
                "last_7d": users_7d,
            },
            "agents": {
                "total": total_agents,
                "active_7d": active_agents_7d,
            },
            "messages": {
                "today": messages_today,
                "total": total_messages,
            },
            "sessions": {
                "total": total_sessions,
                "active_7d": active_sessions_7d,
            },
            "channels": {
                ch: cnt for ch, cnt in channel_stats
            },
            "active": {
                "count": len(active_today),
                "recent": list(recent_activity.values()),
            },
        }
    finally:
        db.close()
