"""
用户 API 路由
用户注册、登录等功能
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session
from datetime import datetime, date
from pydantic import BaseModel
from typing import Optional, List
import logging
import re
import time

from config import settings
from models.database import get_db, User, AgentProfile, ChatHistory, FilePermission
from models.group import GroupMoments
from utils.auth import hash_password, verify_password, create_jwt_token, get_current_user, get_current_user_id, needs_rehash
from services.agent_init_service import agent_init_service
from services.storage_service import get_storage_service
from services.permission_service import PermissionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user", tags=["User"])


# ==========================================
# VFS File Manager API (Phase 2A)
# ==========================================

def _get_agent_or_404(db: Session, agent_hash: str, user_id: int) -> AgentProfile:
    """Verify agent ownership, raise 404 if not found."""
    agent = db.query(AgentProfile).filter(
        AgentProfile.hash == agent_hash,
        AgentProfile.user_id == user_id
    ).first()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def _vfs_path_to_cos_key(agent_hash: str, vfs_path: str) -> str:
    """
    Convert VFS path to COS key.

    VFS /workspace/... → COS agents/{hash}/workspace/...
    VFS /               → COS agents/{hash}/
    """
    normalized = vfs_path.lstrip("/")
    if normalized:
        return f"{settings.TENCENT_COS_PREFIX}agents/{agent_hash}/{normalized}"
    return f"{settings.TENCENT_COS_PREFIX}agents/{agent_hash}/"


class VFSEntry(BaseModel):
    name: str
    type: str  # "dir" | "file"
    size: int
    mtime: float
    content_type: Optional[str] = None


def _parse_cos_date(date_str: str) -> float:
    """Parse COS LastModified string to Unix epoch float."""
    if not date_str:
        return 0.0
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return dt.timestamp()
    except Exception:
        return 0.0


@router.get("/agents/{hash}/vfs")
async def list_vfs_dir(
    hash: str,
    path: str = "/",
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    List VFS directory contents.

    Returns files and subdirectories under the given VFS path.
    """
    _get_agent_or_404(db, hash, user_id)

    cos_prefix = _vfs_path_to_cos_key(hash, path)
    if not cos_prefix.endswith("/"):
        cos_prefix += "/"

    storage = get_storage_service()
    objects = storage.list_objects(cos_prefix, max_keys=1000)
    if objects is None:
        return JSONResponse(content={"entries": [], "path": path})

    # Parse direct children
    entries: List[VFSEntry] = []
    base_len = len(cos_prefix)

    seen: set = set()
    for obj in objects:
        key = obj["Key"]
        rel_path = key[base_len:].lstrip("/")

        # Only direct children (no nested)
        if "/" in rel_path:
            dir_name = rel_path.split("/")[0]
            if dir_name and dir_name not in seen:
                seen.add(dir_name)
                entries.append(VFSEntry(
                    name=dir_name,
                    type="dir",
                    size=4096,
                    mtime=0,
                    content_type=None
                ))
        else:
            name = rel_path
            if name and name not in seen:
                seen.add(name)
                entries.append(VFSEntry(
                    name=name,
                    type="file",
                    size=int(obj.get("Size", 0) or 0),
                    mtime=_parse_cos_date(obj.get("LastModified", "")),
                    content_type=obj.get("ContentType", "application/octet-stream")
                ))

    # Sort: dirs first, then by name
    entries.sort(key=lambda e: (e.type != "dir", e.name))
    return JSONResponse(content={"entries": [e.model_dump() for e in entries], "path": path})


@router.get("/agents/{hash}/vfs/url")
async def get_vfs_presigned_url(
    hash: str,
    path: str = Query(..., description="VFS file path, e.g. /workspace/images/photo.png"),
    mode: str = Query("download", description="view|download"),
    expires: int = Query(86400, ge=1, le=604800, description="URL expiry in seconds"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Get a presigned download URL for a VFS file.

    mode=view: attempts inline display (response-content-disposition=inline)
    mode=download: forces download (response-content-disposition=attachment)
    """
    _get_agent_or_404(db, hash, user_id)

    cos_key = _vfs_path_to_cos_key(hash, path)
    storage = get_storage_service()

    # Build public URL
    public_url = storage.get_object_public_url(cos_key)

    # Generate presigned GET URL
    presigned_url = storage.generate_presigned_get_url(public_url, expired=expires)

    # Note: Tencent COS presigned GET URLs don't natively support
    # response-content-disposition as a query param. For inline viewing,
    # the client should open the URL directly (browser handles based on Content-Type).
    # The mode hint is returned for client-side awareness.
    return JSONResponse(content={
        "url": presigned_url,
        "method": "GET",
        "expires_at": int(time.time()) + expires,
        "mode": mode,
        "key": cos_key
    })


class UploadUrlRequest(BaseModel):
    path: str  # VFS path e.g. /workspace/images/photo.png
    content_type: str = "application/octet-stream"


@router.post("/agents/{hash}/vfs/url-upload")
async def get_vfs_upload_url(
    hash: str,
    body: UploadUrlRequest,
    expires: int = Query(3600, ge=60, le=86400, description="URL expiry in seconds"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Get a presigned PUT URL for uploading a file to VFS.

    The client should PUT the file directly to the returned URL.
    """
    _get_agent_or_404(db, hash, user_id)

    cos_key = _vfs_path_to_cos_key(hash, body.path)
    storage = get_storage_service()

    presigned_url = storage.generate_presigned_put_url(cos_key, expired=expires)

    return JSONResponse(content={
        "url": presigned_url,
        "method": "PUT",
        "expires_at": int(time.time()) + expires,
        "key": cos_key,
        "content_type": body.content_type
    })


class MkdirRequest(BaseModel):
    path: str  # VFS directory path e.g. /workspace/images


@router.post("/agents/{hash}/vfs/mkdir")
async def create_vfs_dir(
    hash: str,
    body: MkdirRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Create a VFS directory.

    COS doesn't have real directories. Creates a zero-byte object with
    a trailing "/" in the key to represent the directory.
    """
    _get_agent_or_404(db, hash, user_id)

    cos_key = _vfs_path_to_cos_key(hash, body.path)
    if not cos_key.endswith("/"):
        cos_key += "/"

    storage = get_storage_service()
    storage.put_object(cos_key, b"")

    logger.info(f"[VFS] Created directory: {cos_key}")
    return JSONResponse(content={"status": "ok", "path": body.path})


@router.delete("/agents/{hash}/vfs/rm")
async def delete_vfs_path(
    hash: str,
    path: str = Query(..., description="VFS path to delete"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Delete a VFS file or empty directory.

    For directories, only succeeds if the directory is empty.
    """
    _get_agent_or_404(db, hash, user_id)

    cos_key = _vfs_path_to_cos_key(hash, path)
    if cos_key.endswith("/"):
        # Verify it's empty
        storage = get_storage_service()
        objects = storage.list_objects(cos_key, max_keys=2)
        if objects and len(objects) > 0:
            raise HTTPException(status_code=400, detail="Directory not empty, cannot delete")

    storage = get_storage_service()
    success = storage.delete_file_by_key(cos_key)
    if not success:
        raise HTTPException(status_code=500, detail="Delete failed")

    logger.info(f"[VFS] Deleted: {cos_key}")
    return JSONResponse(content={"status": "ok", "path": path})


class MoveRequest(BaseModel):
    from_path: str
    to_path: str


@router.post("/agents/{hash}/vfs/mv")
async def move_vfs_path(
    hash: str,
    body: MoveRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Rename or move a VFS file/directory.

    Implementation: copy to new key + delete old key.
    """
    _get_agent_or_404(db, hash, user_id)

    from_key = _vfs_path_to_cos_key(hash, body.from_path)
    to_key = _vfs_path_to_cos_key(hash, body.to_path)

    storage = get_storage_service()

    # Read content from source
    content = storage.get_file_content(from_key)
    if content is None:
        raise HTTPException(status_code=404, detail="Source not found")

    # Write to destination
    storage.put_object(to_key, content)

    # Delete source
    storage.delete_file_by_key(from_key)

    logger.info(f"[VFS] Moved: {from_key} → {to_key}")
    return JSONResponse(content={"status": "ok", "from_path": body.from_path, "to_path": body.to_path})


class PermissionRequest(BaseModel):
    path: str
    permission: str  # "read" | "readwrite" | "none"


@router.patch("/agents/{hash}/vfs/permissions")
async def set_vfs_permission(
    hash: str,
    body: PermissionRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Set file permission for an Agent on a VFS path.

    permission: "read" | "readwrite" | "none"
    """
    _get_agent_or_404(db, hash, user_id)

    valid_perms = {"read", "readwrite", "none"}
    if body.permission not in valid_perms:
        raise HTTPException(status_code=400, detail=f"Invalid permission. Must be one of: {valid_perms}")

    svc = PermissionService(agent_hash=hash, db=db)
    ok = svc.grant_permission(body.path, body.permission)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to set permission")

    return JSONResponse(content={"status": "ok", "path": body.path, "permission": body.permission})


class FileEvent(BaseModel):
    type: str  # e.g. "create", "modify", "delete"
    path: str
    timestamp: int  # Unix epoch ms


class FileEventsRequest(BaseModel):
    events: List[FileEvent]


@router.post("/agents/{hash}/vfs/events")
async def receive_vfs_events(
    hash: str,
    body: FileEventsRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Receive file operation event notifications from Desktop.

    Currently a no-op: logs events and returns 200.
    Future use: index events for change feeds / webhooks.
    """
    _get_agent_or_404(db, hash, user_id)

    for event in body.events:
        logger.info(f"[VFS Events] agent={hash} type={event.type} path={event.path} ts={event.timestamp}")

    return JSONResponse(content={"status": "ok", "received": len(body.events)})


class AgentSettingsRequest(BaseModel):
    alias: Optional[str] = None
    is_pinned: Optional[bool] = None
    is_dnd: Optional[bool] = None
    permission_mode: Optional[str] = None


@router.patch("/agents/{hash}/settings")
async def update_agent_settings(
    hash: str,
    body: AgentSettingsRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Update agent display settings (sync from Desktop).

    - alias: maps to AgentProfile.name
    - is_pinned: Desktop pin state
    - is_dnd: Desktop do-not-disturb state
    - permission_mode: permission mode string
    """
    agent = _get_agent_or_404(db, hash, user_id)

    if body.alias is not None:
        agent.name = body.alias
    if body.is_pinned is not None:
        agent.is_pinned = body.is_pinned
    if body.is_dnd is not None:
        agent.is_dnd = body.is_dnd
    if body.permission_mode is not None:
        agent.permission_mode = body.permission_mode

    agent.updated_at = datetime.utcnow()
    db.commit()

    return JSONResponse(content={
        "status": "ok",
        "hash": hash,
        "name": agent.name,
        "is_pinned": agent.is_pinned,
        "is_dnd": agent.is_dnd,
        "permission_mode": agent.permission_mode
    })


class AgentAvatarRequest(BaseModel):
    avatar_url: Optional[str] = None


@router.patch("/agents/{hash}/avatar")
async def update_agent_avatar(
    hash: str,
    body: AgentAvatarRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """Set or clear agent avatar URL."""
    agent = _get_agent_or_404(db, hash, user_id)
    agent.avatar_url = body.avatar_url
    agent.updated_at = datetime.utcnow()
    db.commit()
    return JSONResponse(content={
        "status": "ok",
        "hash": hash,
        "avatar_url": agent.avatar_url,
    })


@router.delete("/agents/{hash}")
async def delete_agent(
    hash: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """Delete an agent by hash."""
    agent = _get_agent_or_404(db, hash, user_id)
    db.delete(agent)
    db.commit()
    return JSONResponse(content={
        "status": "ok",
        "message": f"Agent {hash} deleted"
    })


@router.get("/agents/{hash}/apps")
async def list_agent_apps(
    hash: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    List miniapps available for the agent.

    Currently returns empty list. Future: query apps_service.
    """
    _get_agent_or_404(db, hash, user_id)
    return JSONResponse(content={"apps": []})


@router.get("/agents/{hash}/config")
async def get_agent_config(
    hash: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Get agent configuration: persona, tools, and config files.
    """
    _get_agent_or_404(db, hash, user_id)

    persona = agent_init_service.load_agent_persona(hash)
    tools = agent_init_service.load_agent_tools(hash)
    config = agent_init_service.load_agent_config(hash)

    return JSONResponse(content={
        "status": "ok",
        "hash": hash,
        "persona": persona or "",
        "tools": tools or {"enabled": [], "disabled": []},
        "config": config or {}
    })


class UpdateAgentConfigRequest(BaseModel):
    persona: Optional[str] = None
    tools: Optional[dict] = None
    config: Optional[dict] = None


@router.put("/agents/{hash}/config")
async def update_agent_config(
    hash: str,
    body: UpdateAgentConfigRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    Update agent configuration: persona, tools, and/or config files.
    """
    agent = _get_agent_or_404(db, hash, user_id)

    results = {}
    errors = []

    if body.persona is not None:
        ok = agent_init_service.save_agent_persona(hash, body.persona)
        results["persona"] = "updated" if ok else "unchanged"

    if body.tools is not None:
        ok, err = agent_init_service.save_agent_tools(hash, body.tools)
        if ok:
            results["tools"] = "updated"
        else:
            errors.append(f"tools: {err}")

    if body.config is not None:
        ok, err = agent_init_service.save_agent_config(hash, body.config)
        if ok:
            results["config"] = "updated"
        else:
            errors.append(f"config: {err}")

    agent.updated_at = datetime.utcnow()
    db.commit()

    if errors:
        return JSONResponse(status_code=400, content={
            "status": "partial_success",
            "results": results,
            "errors": errors
        })

    return JSONResponse(content={"status": "ok", "hash": hash, "results": results})


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

        # 创建用户 — P0.4 bcrypt：salt 嵌入 hash 本身，无需单独存
        password_hash = hash_password(password)

        # 直接激活用户（无需管理员审批）
        # 如果需要审批机制，可以设置 is_active=False，需要管理员手动激活
        user = User(
            username=username,
            password_hash=password_hash,
            salt=None,
            password_version=2,
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

        # 验证密码 — P0.4：新签名 verify_password(password, password_hash)
        if not verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail={"status": "error", "message": "用户名或密码错误"})

        # P0.4 透明懒迁移：legacy SHA-256+salt 用户登录成功后自动升级到 bcrypt
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
            user.salt = None
            user.password_version = 2
            db.commit()
            logger.info(f"[User] 密码 hash 懒迁移到 bcrypt: username={username}")

        # 生成 Token
        token = create_jwt_token({"user_id": user.id})

        logger.info(f"[User] 用户登录成功: {username} (id={user.id})")

        # 配置向导：若 .env 标记未完成 → 登录后跳到 /setup
        redirect_to = None
        try:
            from services.setup_service import is_setup_complete
            if user.is_admin and not is_setup_complete(db):
                redirect_to = "/setup"
        except Exception as _e:
            # 配置检测失败不影响登录
            logger.debug(f"[User] setup 检测异常（忽略）: {_e}")

        resp = JSONResponse(content={
            "status": "success",
            "token": token,
            "user_id": user.id,
            "username": user.username,
            "is_admin": user.is_admin,
            "redirect": redirect_to,
        })
        resp.set_cookie(
            key="feclaw_jwt",
            value=token,
            httponly=True,
            max_age=86400 * 30,  # 30 天
            samesite="lax",
            path="/",
        )
        return resp

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

    # 验证当前密码 — P0.4：新签名
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail={"message": "当前密码错误"})

    # 验证新密码长度
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail={"message": "新密码至少 6 位"})

    # 更新密码 — P0.4：bcrypt，salt 嵌入 hash
    user.password_hash = hash_password(body.new_password)
    user.salt = None
    user.password_version = 2
    db.commit()

    return {"status": "success", "message": "密码已修改"}

# ==========================================
# Desktop API (Phase 4a)
# ==========================================

class CreateAgentRequest(BaseModel):
    name: str = ""
    agent_type: str = "classic"


@router.get("/permissions")
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


@router.post("/agents")
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


@router.get("/agents/{agent_hash}")
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


# ==========================================
# User-level Moments API (Phase 5)
# ==========================================

class CreateMomentRequest(BaseModel):
    group_id: str
    kind: str = "manual"
    title: Optional[str] = None
    content: Optional[str] = None
    attachments: Optional[List[dict]] = None


@router.get("/moments", response_model=List[dict])
async def list_user_moments(
    group_id: Optional[str] = Query(None, description="Filter to a specific group"),
    before: Optional[int] = Query(None, description="Unix timestamp — return moments before this time"),
    limit: int = Query(50, ge=1, le=200),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    List moments across all groups the user owns or is a member of.
    Optionally filter to a specific group.
    """
    from services.moments_service import moments_service
    from models.group import Group

    before_dt = datetime.fromtimestamp(before) if before else None

    if group_id:
        # Validate user has access to this group
        group = db.query(Group).filter(Group.id == group_id, Group.deleted_at.is_(None)).first()
        if not group or group.owner_user_id != user_id:
            raise HTTPException(status_code=403, detail="Not authorized to view this group's moments")
        moments = moments_service.get_moments(db, group_id, before=before_dt, limit=limit)
    else:
        moments = moments_service.get_user_moments(db, user_id, before=before_dt, limit=limit)

    return [
        {
            "id": m.id,
            "group_id": m.group_id,
            "agent_hash": m.agent_hash,
            "kind": m.kind,
            "title": m.title,
            "content": m.content,
            "attachments": m.attachments or [],
            "created_at": int(m.created_at.timestamp()),
        }
        for m in moments
    ]


@router.post("/moments", response_model=dict)
async def create_user_moment(
    body: CreateMomentRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Manually create a moment for a specific group.
    The user must own the group.
    """
    from services.moments_service import moments_service
    from models.group import Group

    # Verify user owns the group
    group = db.query(Group).filter(
        Group.id == body.group_id,
        Group.owner_user_id == user_id,
        Group.deleted_at.is_(None)
    ).first()
    if not group:
        raise HTTPException(status_code=403, detail="Not authorized to post to this group")

    moment = moments_service.create_moment(
        db=db,
        group_id=body.group_id,
        agent_hash=None,  # manual post from user, no agent
        kind=body.kind,
        title=body.title,
        content=body.content,
        attachments=body.attachments,
    )

    # WS push (fire-and-forget)
    import asyncio
    try:
        asyncio.create_task(moments_service.push_moments_event(body.group_id, moment))
    except Exception:
        pass

    return JSONResponse(content={"status": "ok", "moment_id": moment.id})


# ==========================================
# Search Aggregation API (Phase 7)
# ==========================================

@router.get("/search")
async def search_all(
    q: str,
    sources: str = "chat,vfs,moments,textbook",
    limit: int = 10,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    全平台搜索聚合端点。

    q: 搜索词（自然语言或关键词）
    sources: 逗号分隔的数据源，支持 chat|vfs|moments|textbook|miniapps|local
    limit: 每源返回结果数

    并行搜索多数据源，3s 超时，合并返回。
    """
    import asyncio
    import time as time_module

    from services.vector_search_service import VectorSearchService
    from services.moments_service import moments_service
    from models.group import Group, GroupMember

    t0 = time_module.time()
    source_list = [s.strip() for s in sources.split(",") if s.strip()]
    if not source_list:
        source_list = ["chat", "vfs", "moments", "textbook"]

    TIMEOUT = 3.0

    async def _search_chat() -> dict:
        """Search ChatHistory by user_id + content LIKE."""
        try:
            results = await asyncio.wait_for(
                _sync_search_chat(db, user_id, q, limit),
                timeout=TIMEOUT,
            )
            return {"status": "ok", "items": results}
        except asyncio.TimeoutError:
            return {"status": "timeout", "items": []}
        except Exception as e:
            return {"status": "error", "message": str(e), "items": []}

    async def _search_vfs() -> dict:
        """Search user's agents' VFS KB indexes via VectorSearchService."""
        try:
            agents = db.query(AgentProfile).filter(
                AgentProfile.user_id == user_id
            ).all()
            if not agents:
                return {"status": "ok", "items": []}

            svc = VectorSearchService()
            tasks = []
            for agent in agents:
                tasks.append(
                    svc.search_public_with_quality(
                        query=q,
                        top_k=limit,
                        agent_hash=agent.hash,
                        min_score=0.05,
                    )
                )
            results_list = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=TIMEOUT,
            )
            items = []
            seen = set()
            for agent, results in zip(agents, results_list):
                if isinstance(results, Exception):
                    continue
                for r in results:
                    key = r.get("key", "")
                    if key in seen:
                        continue
                    seen.add(key)
                    meta = r.get("metadata", {})
                    items.append({
                        "id": key,
                        "agent_hash": agent.hash,
                        "agent_name": agent.name or agent.hash,
                        "snippet": meta.get("text", "")[:200],
                        "score": r.get("score", 0),
                        "timestamp": 0,
                        "source": "vfs",
                    })
            return {"status": "ok", "items": items[:limit]}
        except asyncio.TimeoutError:
            return {"status": "timeout", "items": []}
        except Exception as e:
            return {"status": "error", "message": str(e), "items": []}

    async def _search_moments() -> dict:
        """Search GroupMoments by user's groups, text match on title + content."""
        try:
            results = await asyncio.wait_for(
                _sync_search_moments(db, user_id, q, limit),
                timeout=TIMEOUT,
            )
            return {"status": "ok", "items": results}
        except asyncio.TimeoutError:
            return {"status": "timeout", "items": []}
        except Exception as e:
            return {"status": "error", "message": str(e), "items": []}

    async def _search_textbook() -> dict:
        """Search textbook indexes via VectorSearchService.search_quality_textbook."""
        try:
            svc = VectorSearchService()
            results = await asyncio.wait_for(
                svc.search_quality_textbook(query=q, top_k=limit),
                timeout=TIMEOUT,
            )
            items = []
            for r in results:
                meta = r.get("metadata", {})
                items.append({
                    "id": r.get("key", ""),
                    "agent_hash": "",
                    "agent_name": "",
                    "snippet": meta.get("text", "")[:200],
                    "score": r.get("score", 0),
                    "timestamp": 0,
                    "source": "textbook",
                })
            return {"status": "ok", "items": items}
        except asyncio.TimeoutError:
            return {"status": "timeout", "items": []}
        except Exception as e:
            return {"status": "error", "message": str(e), "items": []}

    async def _search_miniapps() -> dict:
        """Search AgentProfile apps list by name + description (simple text match)."""
        try:
            results = await asyncio.wait_for(
                _sync_search_miniapps(db, user_id, q, limit),
                timeout=TIMEOUT,
            )
            return {"status": "ok", "items": results}
        except asyncio.TimeoutError:
            return {"status": "timeout", "items": []}
        except Exception as e:
            return {"status": "error", "message": str(e), "items": []}

    async def _search_local() -> dict:
        """Placeholder for local file search — Desktop will add later."""
        return {"status": "ok", "items": []}

    # Build task map
    task_map = {
        "chat": _search_chat,
        "vfs": _search_vfs,
        "moments": _search_moments,
        "textbook": _search_textbook,
        "miniapps": _search_miniapps,
        "local": _search_local,
    }

    # Launch enabled sources in parallel
    tasks = {}
    for src in source_list:
        if src in task_map:
            tasks[src] = asyncio.create_task(task_map[src]())

    # Collect results (wait up to TIMEOUT total)
    results_out = {}
    if tasks:
        done, pending = await asyncio.wait(
            tasks.values(),
            timeout=TIMEOUT,
        )
        for src, task in tasks.items():
            if task in done:
                try:
                    results_out[src] = task.result()
                except Exception as e:
                    results_out[src] = {"status": "error", "message": str(e), "items": []}
            else:
                task.cancel()
                results_out[src] = {"status": "timeout", "items": []}

    # Fill missing sources with error
    for src in source_list:
        if src not in results_out:
            results_out[src] = {"status": "error", "message": "not run", "items": []}

    elapsed_ms = int((time_module.time() - t0) * 1000)
    return JSONResponse(content={
        "query": q,
        "results": results_out,
        "elapsed_ms": elapsed_ms,
    })


# ---- Sync helpers (called from async context) ----

def _sync_search_chat(db: Session, user_id: int, q: str, limit: int) -> list:
    """Search ChatHistory rows by content LIKE. Runs in thread pool."""
    import asyncio
    from sqlalchemy import orm

    pattern = f"%{q}%"
    query = db.query(
        ChatHistory.id,
        ChatHistory.agent_hash,
        ChatHistory.content,
        ChatHistory.created_at,
    ).filter(
        ChatHistory.user_id == user_id,
        ChatHistory.content.ilike(pattern),
    ).order_by(
        ChatHistory.created_at.desc()
    ).limit(limit)

    items = []
    # Resolve agent names
    agent_hashes = set()
    rows = query.all()
    for row in rows:
        agent_hashes.add(row.agent_hash)

    agent_names = {}
    if agent_hashes:
        profiles = db.query(AgentProfile.hash, AgentProfile.name).filter(
            AgentProfile.hash.in_(agent_hashes)
        ).all()
        agent_names = {p.hash: p.name or p.hash for p in profiles}

    for row in rows:
        content = row.content or ""
        snippet = content[:200] if len(content) > 200 else content
        items.append({
            "id": f"msg-{row.id}",
            "agent_hash": row.agent_hash,
            "agent_name": agent_names.get(row.agent_hash, row.agent_hash),
            "snippet": snippet,
            "score": 0.5,  # Simple text match, no vector score
            "timestamp": int(row.created_at.timestamp()) if row.created_at else 0,
            "source": "chat",
        })
    return items


def _sync_search_moments(db: Session, user_id: int, q: str, limit: int) -> list:
    """Search GroupMoments rows visible to user by title + content LIKE."""
    from models.group import Group

    # Get group IDs the user has access to
    owned = db.query(Group.id).filter(
        Group.owner_user_id == user_id,
        Group.deleted_at.is_(None)
    ).all()
    group_ids = [g.id for g in owned]

    if not group_ids:
        return []

    # Fetch all moments for these groups, filter in Python
    rows = db.query(GroupMoments).filter(
        GroupMoments.group_id.in_(group_ids),
    ).order_by(
        GroupMoments.created_at.desc()
    ).limit(200).all()

    q_lower = q.lower()
    items = []
    for row in rows:
        title = row.title or ""
        content = row.content or ""
        if q_lower not in title.lower() and q_lower not in content.lower():
            continue
        snippet = (title + " " + content)[:200]
        items.append({
            "id": row.id,
            "agent_hash": row.agent_hash or "",
            "agent_name": "",
            "snippet": snippet,
            "score": 0.5,
            "timestamp": int(row.created_at.timestamp()) if row.created_at else 0,
            "source": "moments",
        })
        if len(items) >= limit:
            break

    return items


def _sync_search_miniapps(db: Session, user_id: int, q: str, limit: int) -> list:
    """Search AgentProfile apps list (name + description) by text match."""
    agents = db.query(AgentProfile).filter(
        AgentProfile.user_id == user_id
    ).all()

    items = []
    q_lower = q.lower()
    for agent in agents:
        # apps field not yet implemented — placeholder always empty
        pass
    return items
