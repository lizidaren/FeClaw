"""
OAuth 路由
处理 OAuth 认证流程的 HTTP 接口
"""

import secrets
import time as _time
from datetime import datetime
from urllib.parse import urlparse
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from fastapi.responses import RedirectResponse, JSONResponse
import logging

from sqlalchemy import or_

from config import settings

from services.oauth_service import oauth_service
from models.database import get_db, SessionLocal, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/oauth", tags=["OAuth"])

# OAuth state 通过加密 Cookie 存储，不依赖服务端内存
# cookie 名：oauth_state_{state[:16]}（每个 state 独立 cookie，避免多标签页冲突）


STATE_COOKIE_MAX_AGE = 600  # 10 分钟


def _is_safe_redirect(url: str) -> str:
    if not url or url.startswith('/'):
        return url
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    allowed_hosts = {'localhost', '127.0.0.1', '::1', 'feclaw.chat', 'firstentrance.net', 'app.firstentrance.net', 'feclaw.lizidaren.cn'}
    if parsed.netloc.split(':')[0] in allowed_hosts:
        return url
    return '/'


@router.get("/login")
async def oauth_login(request: Request):
    """
    OAuth 登录入口
    重定向到 Platform 登录页面
    """
    # 生成随机 state，防止 CSRF 攻击
    state = secrets.token_urlsafe(32)

    # 写入 Cookie（不依赖服务端内存，多 worker/重启/多标签页均安全）
    response = RedirectResponse(url=oauth_service.get_authorize_url(state), status_code=302)
    cookie_name = f"oauth_state_{state[:16]}"
    response.set_cookie(
        key=cookie_name,
        value=state,
        max_age=STATE_COOKIE_MAX_AGE,
        path="/",
        secure=False,
        httponly=True,
        samesite="lax"
    )
    if settings.FECLAW_DOMAIN:
        response.headers.append("Set-Cookie", f"{cookie_name}={state}; Path=/; Domain=.{settings.FECLAW_DOMAIN}; Max-Age={STATE_COOKIE_MAX_AGE}; HttpOnly; SameSite=Lax")
    logger.info(f"OAuth login initiated, state={state[:16]}...")

    return response


@router.get("/callback")
async def oauth_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...)
):
    """OAuth 回调处理（含性能日志）"""
    t0 = _time.time()

    # 从 Cookie 读取 state，不依赖服务端内存
    cookie_name = f"oauth_state_{state[:16]}"
    cookie_state = request.cookies.get(cookie_name)
    if not cookie_state or cookie_state != state:
        logger.warning(f"OAuth callback: state mismatch (cookie={cookie_state[:16] if cookie_state else 'missing'}, param={state[:16]}...)")
        raise HTTPException(status_code=400, detail="Invalid state parameter")
    logger.info(f"[PERF] state lookup (cookie): {(_time.time()-t0)*1000:.0f}ms")

    # 授权码换 token
    t1 = _time.time()
    try:
        token_data = await oauth_service.exchange_code_for_token(code)
    except Exception as e:
        logger.error(f"[PERF] Platform token exchange failed after {(_time.time()-t1)*1000:.0f}ms: {e}")
        raise HTTPException(status_code=502, detail="Platform 认证服务暂时不可用，请稍后重试")
    logger.info(f"[PERF] exchange_code_for_token: {(_time.time()-t1)*1000:.0f}ms")

    if token_data is None:
        raise HTTPException(status_code=400, detail="Failed to exchange code for token")

    access_token = token_data.get("access_token")

    # 验证 id_token
    id_token = token_data.get("id_token")
    t2 = _time.time()
    if id_token:
        id_payload = await oauth_service.verify_platform_jwt(id_token)
        if not id_payload:
            logger.warning(f"[PERF] verify_platform_jwt failed after {(_time.time()-t2)*1000:.0f}ms (skipped)")
        else:
            logger.info(f"[PERF] verify_platform_jwt: {(_time.time()-t2)*1000:.0f}ms")

    # 获取用户信息
    t3 = _time.time()
    try:
        user_info = await oauth_service.get_userinfo(access_token)
    except Exception as e:
        logger.error(f"Platform userinfo fetch failed: {e}")
        raise HTTPException(status_code=502, detail="Platform 用户信息服务暂时不可用，请稍后重试")

    if user_info is None:
        raise HTTPException(status_code=400, detail="Failed to get user info")

    # 创建或更新本地用户
    db = SessionLocal()
    try:
        platform_user_id = user_info.get("sub") or user_info.get("user_id")
        username = user_info.get("username") or user_info.get("name") or f"platform_{platform_user_id}"

        # 优先按 platform_user_id 匹配，再按 username 匹配
        existing = db.query(User).filter(
            or_(User.platform_user_id == platform_user_id, User.username == username)
        ).first()

        if existing:
            # 更新现有用户信息
            existing.platform_user_id = platform_user_id
            existing.email = user_info.get("email", existing.email)
            existing.is_admin = user_info.get("is_admin", False) or username == "admin"
            db.commit()
            db.refresh(existing)
            user = existing
            logger.info(f"Updated existing user from OAuth: {username}")
        else:
            # 创建新用户
            # 注意：本地用户不需要密码，因为认证由 Platform 处理
            from utils.auth import generate_salt, hash_password
            salt = generate_salt()
            dummy_password = hash_password(secrets.token_hex(32), salt)

            is_admin = user_info.get("is_admin", False) or username == "admin"
            user = User(
                username=username,
                platform_user_id=platform_user_id,
                password_hash=dummy_password,
                salt=salt,
                is_admin=is_admin
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            logger.info(f"Created new user from OAuth: {username}")
    finally:
        db.close()

    # 创建本地 JWT
    local_jwt = oauth_service.create_local_jwt({
        "sub": user.id,
        "username": user.username,
        "email": user_info.get("email"),
        "auth_method": "platform"
    })

    # 重定向到前端，携带 token
    redirect_to = "/dashboard"

    # 构建 redirect URL，携带 token
    response = RedirectResponse(url=f"{redirect_to}?token={local_jwt}")

    # 同时设置 cookie（共享跨子域名）
    domain = f".{settings.FECLAW_DOMAIN}"
    response.set_cookie(
        key="feclaw_jwt",
        value=local_jwt,
        secure=True,
        samesite="lax",
        path="/",
        domain=domain,
        max_age=settings.JWT_EXPIRE_HOURS * 3600,
    )

    return response


@router.post("/refresh")
async def oauth_refresh(
    request: Request,
    db = Depends(get_db)
):
    """
    刷新 OAuth token
    需要 Authorization header 或 cookie 中的 token
    """
    # 从 header 或 cookie 获取 token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.cookies.get("feclaw_jwt")

    if not token:
        raise HTTPException(status_code=401, detail="No token provided")

    # 解码当前 token
    from utils.auth import decode_jwt_token
    payload = decode_jwt_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    # 创建新的 token
    new_token = oauth_service.create_local_jwt({
        "sub": payload.get("sub"),
        "username": payload.get("username"),
        "email": payload.get("email"),
        "auth_method": payload.get("auth_method", "platform")
    })

    return JSONResponse(content={
        "status": "success",
        "token": new_token
    })


@router.post("/logout")
async def oauth_logout(request: Request):
    """
    OAuth 注销
    清除本地 token cookie
    """
    response = JSONResponse(content={
        "status": "success",
        "message": "Logged out successfully"
    })

    # 清除 cookie
    response.delete_cookie(key="feclaw_jwt")

    return response


@router.get("/me")
async def oauth_me(
    request: Request,
    db = Depends(get_db)
):
    """
    获取当前登录用户信息
    """
    # 从 header 或 cookie 获取 token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.cookies.get("feclaw_jwt")

    if not token:
        raise HTTPException(status_code=401, detail="No token provided")

    # 解码 token
    from utils.auth import decode_jwt_token
    payload = decode_jwt_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    # 获取用户
    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid user ID in token")
    user = db.query(User).filter(User.id == user_id_int).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    return JSONResponse(content={
        "status": "success",
        "user": {
            "id": user.id,
            "username": user.username,
            "is_admin": user.is_admin,
            "created_at": user.created_at.isoformat() if user.created_at else None
        }
    })