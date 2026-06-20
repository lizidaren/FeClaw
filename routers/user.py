"""
用户 API 路由
用户注册、登录等功能
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime, date
from pydantic import BaseModel
import logging
import re

from config import settings
from models.database import get_db, User, AgentProfile, ChatHistory
from utils.auth import generate_salt, hash_password, verify_password, create_jwt_token, get_current_user, get_current_user_id
from services.agent_init_service import agent_init_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user", tags=["User"])


@router.post("/register")
async def register_user(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    用户自助注册

    参数：
    - username: 用户名（3-32字符，字母数字下划线）
    - password: 密码（6-64字符）
    - email: 邮箱（可选）

    返回：
    - user_id: 用户ID
    - requires_approval: 是否需要管理员审批
    - message: 提示信息
    """
    # OAuth 已启用时禁止本地注册
    if settings.OAUTH_ENABLED:
        return JSONResponse(
            status_code=403,
            content={
                "status": "error",
                "error": "oauth_required",
                "message": "请通过 Platform OAuth 注册/登录",
                "oauth_url": "/oauth/login"
            }
        )

    try:
        body = await request.json()
        username = body.get("username", "").strip()
        password = body.get("password", "")
        email = body.get("email", "").strip() or None

        # 验证用户名
        if not username:
            raise HTTPException(status_code=400, detail={"status": "error", "message": "用户名不能为空"})

        if len(username) < 3 or len(username) > 32:
            raise HTTPException(status_code=400, detail={"status": "error", "message": "用户名长度需在3-32字符之间"})

        if not re.match(r'^[a-zA-Z0-9_]+$', username):
            raise HTTPException(status_code=400, detail={"status": "error", "message": "用户名只能包含字母、数字和下划线"})

        # 检查用户名是否已存在
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            raise HTTPException(status_code=400, detail={"status": "error", "message": "用户名已存在"})

        # 验证密码
        if not password:
            raise HTTPException(status_code=400, detail={"status": "error", "message": "密码不能为空"})

        if len(password) < 6 or len(password) > 64:
            raise HTTPException(status_code=400, detail={"status": "error", "message": "密码长度需在6-64字符之间"})

        # 验证邮箱（可选）
        if email:
            if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
                raise HTTPException(status_code=400, detail={"status": "error", "message": "邮箱格式不正确"})

            # 检查邮箱是否已存在
            existing_email = db.query(User).filter(User.email == email).first()
            if existing_email:
                raise HTTPException(status_code=400, detail={"status": "error", "message": "邮箱已被注册"})

        # 创建用户
        salt = generate_salt()
        password_hash = hash_password(password, salt)

        # 直接激活用户（无需管理员审批）
        # 如果需要审批机制，可以设置 is_active=False，需要管理员手动激活
        user = User(
            username=username,
            password_hash=password_hash,
            salt=salt,
            is_admin=False,
            created_at=datetime.utcnow()
        )

        db.add(user)
        db.commit()
        db.refresh(user)

        logger.info(f"[User] 新用户注册成功: {username} (id={user.id})")

        # 生成 JWT Token（注册后自动登录）
        token = create_jwt_token({"user_id": user.id})

        return JSONResponse(content={
            "status": "success",
            "user_id": user.id,
            "username": user.username,
            "requires_approval": False,
            "message": "注册成功",
            "token": token
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[User] 注册失败: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail={"status": "error", "message": "注册失败，请稍后重试"})


@router.post("/login")
async def login_user(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    用户登录

    参数：
    - username: 用户名
    - password: 密码

    返回：
    - token: JWT Token
    - user_id: 用户ID
    """
    # OAuth 已启用时禁止本地登录
    if settings.OAUTH_ENABLED:
        return JSONResponse(
            status_code=403,
            content={
                "status": "error",
                "error": "oauth_required",
                "message": "请通过 Platform OAuth 登录",
                "oauth_url": "/oauth/login"
            }
        )

    from utils.auth import verify_password

    try:
        body = await request.json()
        username = body.get("username", "").strip()
        password = body.get("password", "")

        if not username or not password:
            raise HTTPException(status_code=400, detail={"status": "error", "message": "用户名和密码不能为空"})

        # 查找用户
        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise HTTPException(status_code=401, detail={"status": "error", "message": "用户名或密码错误"})

        # 验证密码
        if not verify_password(password, user.salt, user.password_hash):
            raise HTTPException(status_code=401, detail={"status": "error", "message": "用户名或密码错误"})

        # 生成 Token
        token = create_jwt_token({"user_id": user.id})

        logger.info(f"[User] 用户登录成功: {username} (id={user.id})")

        return JSONResponse(content={
            "status": "success",
            "token": token,
            "user_id": user.id,
            "username": user.username,
            "is_admin": user.is_admin
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[User] 登录失败: {e}")
        raise HTTPException(status_code=500, detail={"status": "error", "message": "登录失败，请稍后重试"})


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """修改密码"""
    from utils.auth import verify_password

    # 验证当前密码
    if not verify_password(body.current_password, user.salt, user.password_hash):
        raise HTTPException(status_code=400, detail={"message": "当前密码错误"})

    # 验证新密码长度
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail={"message": "新密码至少 6 位"})

    # 更新密码
    user.salt = generate_salt()
    user.password_hash = hash_password(body.new_password, user.salt)
    db.commit()

    return {"status": "success", "message": "密码已修改"}

# ==========================================
# Desktop API (Phase 4a)
# ==========================================

class CreateAgentRequest(BaseModel):
    name: str = ""
    agent_type: str = "classic"


@router.get("/api/user/permissions")
async def get_user_permissions(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    获取用户权限和配额信息

    返回用户的 tier、特性配额和使用统计。
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail={"status": "unauthorized"})

    # 统计
    agent_count = db.query(AgentProfile).filter(AgentProfile.user_id == user_id).count()
    group_count = 0  # 暂不支持 group

    # 今日消息数
    today = date.today()
    today_messages = db.query(ChatHistory).filter(
        ChatHistory.user_id == user_id,
        ChatHistory.created_at >= datetime.combine(today, datetime.min.time())
    ).count()

    return JSONResponse(content={
        "tier": user.tier or "pro",
        "user_id": user.id,
        "username": user.username,
        "features": {
            "max_agents": -1,
            "max_groups": -1,
            "storage_bytes": -1,
            "daily_message_limit": -1,
            "available_models": [],
            "permission_modes": ["disabled", "strict", "balanced", "relaxed", "full"],
            "channels": ["web", "desktop", "wechat"],
            "features_enabled": {
                "group_chat": True,
                "moments": True,
                "mini_programs": True,
                "mcp_tools": True,
                "local_file_index": False,
                "wechat_channel": True,
                "mobile_app": False
            }
        },
        "usage": {
            "agent_count": agent_count,
            "group_count": group_count,
            "today_messages": today_messages
        }
    })


@router.post("/api/user/agents")
async def create_agent(
    request: Request,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    为当前用户创建一个新 Agent

    请求体:
        name: Agent 名称（可选）
        agent_type: "classic" | "im"（默认 "classic"）
    """
    body = await request.json()
    name = body.get("name", "")
    agent_type = body.get("agent_type", "classic")

    if agent_type not in ("classic", "im"):
        raise HTTPException(status_code=400, detail={"message": "agent_type must be 'classic' or 'im'"})

    try:
        agent = agent_init_service.create_agent(db, user_id, name=name)
        agent.agent_type = agent_type
        db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return JSONResponse(content={
        "hash": agent.hash,
        "name": agent.name,
        "description": agent.description
    })


@router.get("/api/user/agents/{agent_hash}")
async def get_agent_by_hash(
    agent_hash: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    通过 hash 获取用户的 Agent 详情

    验证 agent 属于当前用户。
    """
    agent = db.query(AgentProfile).filter(
        AgentProfile.hash == agent_hash,
        AgentProfile.user_id == user_id
    ).first()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    from utils.auth import format_timestamp
    return JSONResponse(content={
        "hash": agent.hash,
        "name": agent.name,
        "description": agent.description,
        "agent_type": agent.agent_type or "classic",
        "created_at": format_timestamp(agent.created_at) if agent.created_at else None,
        "avatar_url": agent.avatar_url
    })
