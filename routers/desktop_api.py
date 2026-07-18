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
from utils.oauth_helpers import issue_token_pair_for_platform_user

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
    将 Platform access_token 兑换为 FeClaw JWT（Desktop client 兼容入口）。

    与 Mobile `POST /api/oauth/exchange` 共用同一套 helper（`utils/oauth_helpers`），
    这里只返 access_token + user_id + username，**不**发 refresh_token —— 保留旧 Desktop
    客户端契约，避免破坏现有版本。
    """
    # 1. 通过 Platform /api/auth/me 验证 access_token
    user_info = await _verify_platform_token(body.platform_token)

    # 2. 复用 Mobile /exchange 的 helper（含找/建用户 + 签 access+refresh）
    #    Desktop 端契约只取 access / user_id / username，refresh_token 丢弃即可。
    token_pair = issue_token_pair_for_platform_user(db, user_info)

    logger.info(
        f"Desktop auth_exchange: user_id={token_pair['user_id']} "
        f"username={token_pair['username']} (helper 复用自 oauth.exchange)"
    )

    # 3. 保持 Desktop 旧契约字段
    return {
        "token": token_pair["token"],
        "user_id": token_pair["user_id"],
        "username": token_pair["username"],
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
    avatar_url = ""  # avatar URL 需在部署时配置

    # 5. Update agent profile
    agent.avatar_url = avatar_url
    db.commit()

    return {"avatar_url": avatar_url}


@router.get("/api/desktop/messages")
async def get_desktop_messages(
    agent_hash: str,
    after_id: int = 0,
    limit: int = 50,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """增量获取聊天消息。after_id=0 返回最新 limit 条。"""
    from models.database import ChatHistory

    query = db.query(ChatHistory).filter(
        ChatHistory.agent_hash == agent_hash,
        ChatHistory.user_id == user_id,
    )

    if after_id:
        query = query.filter(ChatHistory.id > after_id)
        query = query.order_by(ChatHistory.id.asc())
    else:
        query = query.order_by(ChatHistory.id.desc()).limit(limit)

    messages = query.all()
    if not after_id:
        messages = list(reversed(messages))

    return [
        {
            "id": m.id,
            "agent_hash": m.agent_hash,
            "role": m.role,
            "content": m.content,
            "channel": m.channel,
            "session_id": m.session_id,
            "create_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in messages
    ]
