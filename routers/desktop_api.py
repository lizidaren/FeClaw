"""Desktop 客户端专用 API 端点"""

import httpx
import secrets
import logging
import certifi
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel

from config import settings
from models.database import get_db, User, AgentProfile
from utils.auth import get_current_user_id
from services.oauth_service import oauth_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Desktop API"])


class AuthExchangeRequest(BaseModel):
    """Desktop 客户端 Platform access_token → FeClaw JWT 兑换请求"""
    platform_token: str


async def _verify_platform_token(access_token: str) -> dict:
    """
    通过 Platform 的 /api/auth/me 端点验证 access_token，获取用户信息。

    Platform 的 direct login API 返回的是自签 JWT（非 OIDC id_token），
    因此不走 OAuthService.verify_platform_jwt（那是给 id_token 用的）。
    改用 REST API 调用验证——Platform 自己会 JWT 解码。
    """
    # 用 OAUTH_TOKEN_URL 推导内部 Platform 地址（不走 CDN）
    if settings.OAUTH_TOKEN_URL:
        platform_base = settings.OAUTH_TOKEN_URL.rsplit("/oauth/token", 1)[0].rstrip("/")
    else:
        platform_base = settings.OAUTH_PROVIDER_URL.rstrip("/")
    me_url = f"{platform_base}/api/auth/me"

    async with httpx.AsyncClient(timeout=10.0, verify=certifi.where()) as client:
        resp = await client.get(
            me_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            logger.warning(f"Platform /api/auth/me failed: {resp.status_code} {resp.text[:100]}")
            raise HTTPException(status_code=401, detail="Invalid or expired Platform token")

        data = resp.json()
        user_info = data.get("user", data)
        if not user_info or not user_info.get("id"):
            raise HTTPException(status_code=401, detail="Platform token valid but no user info")

        return user_info


@router.post("/api/desktop/auth_exchange")
async def desktop_auth_exchange(
    body: AuthExchangeRequest,
    db: Session = Depends(get_db),
):
    """
    将 Platform access_token 兑换为 FeClaw JWT。

    Desktop 通过 Platform 的 /api/auth/login 登录后拿到 access_token，
    此端点用该 token 调用 Platform 的 /api/auth/me 验证身份，
    然后创建或关联本地 User 记录，最后签发 FeClaw JWT。
    """
    # 1. 通过 Platform /api/auth/me 验证 access_token
    user_info = await _verify_platform_token(body.platform_token)

    platform_user_id = str(user_info.get("id"))
    username = user_info.get("username") or f"platform_{platform_user_id}"

    # 2. 创建或匹配本地用户（与 oauth_callback 逻辑一致）
    existing = db.query(User).filter(User.platform_user_id == platform_user_id).first()

    if existing:
        existing.is_admin = user_info.get("is_admin", False)
        if user_info.get("email"):
            existing.email = user_info.get("email")
        db.commit()
        db.refresh(existing)
        user = existing
        logger.info(f"Desktop auth: updated user {username}")
    else:
        by_username = db.query(User).filter(User.username == username).first()
        if by_username and by_username.platform_user_id is None:
            by_username.platform_user_id = platform_user_id
            by_username.is_admin = user_info.get("is_admin", False)
            db.commit()
            db.refresh(by_username)
            user = by_username
            logger.info(f"Desktop auth: linked local user {username}")
        elif by_username and by_username.platform_user_id != platform_user_id:
            from utils.auth import generate_salt, hash_password
            salt = generate_salt()
            dummy_password = hash_password(secrets.token_hex(32), salt)
            user = User(
                username=f"{username}_{platform_user_id}",
                platform_user_id=platform_user_id,
                password_hash=dummy_password,
                salt=salt,
                is_admin=False,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            from utils.auth import generate_salt, hash_password
            salt = generate_salt()
            dummy_password = hash_password(secrets.token_hex(32), salt)
            user = User(
                username=username,
                platform_user_id=platform_user_id,
                password_hash=dummy_password,
                salt=salt,
                is_admin=False,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            logger.info(f"Desktop auth: created new user {username}")

    # 3. 签发 FeClaw JWT
    local_jwt = oauth_service.create_local_jwt({
        "sub": user.id,
        "username": user.username,
        "email": user_info.get("email"),
        "auth_method": "platform",
    })

    return {
        "token": local_jwt,
        "user_id": user.id,
        "username": user.username,
    }


@router.get("/api/desktop/agents")
async def list_agents(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """List all agents for the current user (Desktop client)."""
    agents = db.query(AgentProfile).filter(
        AgentProfile.user_id == user_id
    ).all()

    return [
        {
            "hash": a.hash,
            "name": a.name or a.hash,
            "description": a.description or "",
            "agent_type": a.agent_type or "classic",
            "avatar_url": a.avatar_url,
            "status": a.status or "pending",
            "is_default": a.is_default,
            "is_pinned": a.is_pinned or False,
            "is_dnd": a.is_dnd or False,
            "permission_mode": a.permission_mode,
        }
        for a in agents
    ]


class CreateDesktopAgentRequest(BaseModel):
    name: str = ""
    agent_type: str = "classic"


@router.post("/api/desktop/agents")
async def create_desktop_agent(
    body: CreateDesktopAgentRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Create a new agent for the current user (Desktop client)."""
    from services.agent_init_service import agent_init_service

    if body.agent_type not in ("classic", "im"):
        raise HTTPException(status_code=400, detail={"message": "agent_type must be 'classic' or 'im'"})

    try:
        agent = agent_init_service.create_agent(db, user_id, name=body.name)
        agent.agent_type = body.agent_type
        db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "hash": agent.hash,
        "name": agent.name,
        "description": agent.description,
        "agent_type": agent.agent_type,
        "status": agent.status or "pending",
    }


@router.post("/api/desktop/agents/{hash}/avatar")
async def upload_agent_avatar(
    hash: str,
    file: UploadFile = File(...),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Upload avatar image for an agent (Desktop client). Saves to COS at agents/{hash}/avatar.png."""
    # 1. Verify agent ownership
    agent = db.query(AgentProfile).filter(
        AgentProfile.hash == hash,
        AgentProfile.user_id == user_id,
    ).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # 2. Validate and read file
    contents = await file.read()
    if len(contents) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 2MB)")
    ext = "png"
    if file.content_type == "image/jpeg":
        ext = "jpg"
    elif file.content_type == "image/gif":
        ext = "gif"
    elif file.content_type == "image/webp":
        ext = "webp"

    # 3. Save to COS
    cos_key = f"agents/{hash}/avatar.{ext}"
    from services.storage_service import storage
    storage.put_object(cos_key, contents)

    # 4. Return VFS view URL
    avatar_url = f"https://feclaw.lizidaren.cn/api/vfs/view?path={cos_key}"

    # 5. Update agent profile
    agent.avatar_url = avatar_url
    db.commit()

    return {"avatar_url": avatar_url}
