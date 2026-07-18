"""
OAuth 路由
处理 OAuth 认证流程的 HTTP 接口

包含：
- Web OAuth flow：`/login`、`/callback`、`/logout`、`/me`
- Mobile OAuth flow（P0-A-1 + P1-A-2 + P1-A-3）：
    - `POST /api/oauth/exchange`     Platform access_token → FeClaw JWT pair
    - `POST /api/oauth/refresh`      refresh_token → 新 access + refresh
    - `GET  /api/oauth/mobile-login` 生成 Platform authorize URL（Mobile Linking.openURL 用）

CSRF 校验（P0-A-2 修复）：`/callback` 不再静默 fallback；state cookie 缺失或与 query 不一致 → 400。
"""

import secrets
import time as _time
from typing import Optional
from urllib.parse import urlencode, urlparse
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import httpx
import certifi
import logging

from config import settings

from services.oauth_service import oauth_service
from models.database import get_db, SessionLocal, User
from utils.auth_dependencies import (
    get_current_user,
    get_current_token_payload,
)
from utils.oauth_helpers import (
    decode_refresh_token,
    find_or_create_user_from_platform,
    issue_token_pair_for_platform_user,
    sign_access_token,
    sign_refresh_token,
)

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
    allowed_hosts = {'localhost', '127.0.0.1', '::1'}
    if parsed.netloc.split(':')[0] in allowed_hosts:
        return url
    return '/'


@router.get("/login")
async def oauth_login(request: Request):
    """
    OAuth 登录入口
    重定向到 Platform 登录页面
    """
    # 检查 OAuth 是否已配置
    authorize_url = oauth_service.get_authorize_url("dummy")
    if not authorize_url:
        logger.warning("[OAuth] OAuth 未配置，跳转到本地登录页")
        return RedirectResponse(url="/login?error=oauth_not_configured", status_code=302)

    # 生成随机 state，防止 CSRF 攻击
    state = secrets.token_urlsafe(32)

    # 写入 Cookie（不依赖服务端内存，多 worker/重启/多标签页均安全）
    response = RedirectResponse(url=oauth_service.get_authorize_url(state), status_code=302)
    cookie_name = f"oauth_state_{state[:16]}"
    # P2-6 修复：secure=True，使用 set_cookie 的 domain 参数避免双重设置
    response.set_cookie(
        key=cookie_name,
        value=state,
        max_age=STATE_COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="none",
        domain=f".{settings.FECLAW_PUBLIC_URL}" if settings.FECLAW_PUBLIC_URL and settings.FECLAW_SUBDOMAIN_ENABLED else None,
    )
    logger.info(f"OAuth login initiated, state={state[:16]}...")

    return response


@router.get("/callback")
async def oauth_callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(...),
    error: str = Query(None),
    error_description: str = Query(None),
):
    """OAuth 回调处理（含性能日志）"""
    t0 = _time.time()

    # 用户取消授权
    if error:
        logger.info(f"OAuth callback with error: {error} ({error_description})")
        domain = settings.FECLAW_PUBLIC_URL or ""
        base = f"https://{domain}"
        redirect_url = f"{base}/login?error={error}"
        return RedirectResponse(url=redirect_url)

    # 从 Cookie 读取 state，不依赖服务端内存
    # P0-A-2 修复：缺 cookie 或不匹配都视为 CSRF 失败，**不再静默 fallback**
    cookie_name = f"oauth_state_{state[:16]}"
    cookie_state = request.cookies.get(cookie_name)
    if not cookie_state:
        logger.warning(f"OAuth callback: missing state cookie for state={state[:16]}... (cross-site? cookie blocked?)")
        raise HTTPException(status_code=400, detail={
            "status": "invalid_state",
            "message": "Missing OAuth state cookie. 请从同站入口重新发起登录，确保浏览器允许第三方 cookie / SameSite=None Secure。"
        })
    if cookie_state != state:
        logger.warning(f"OAuth callback: state mismatch (cookie={cookie_state[:16]}..., param={state[:16]}...)")
        raise HTTPException(status_code=400, detail={
            "status": "invalid_state",
            "message": "Invalid state parameter (CSRF check failed)."
        })
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

        # 安全匹配：先按 platform_user_id 精准查（P0-1 修复：禁止 or_ 条件）
        existing = db.query(User).filter(User.platform_user_id == platform_user_id).first()

        if existing:
            # 按 platform_user_id 精准匹配 → 更新
            existing.email = user_info.get("email", existing.email)
            existing.is_admin = user_info.get("is_admin", False) or username == "admin"
            db.commit()
            db.refresh(existing)
            user = existing
            logger.info(f"Updated existing user from OAuth: {username}")
        else:
            # 按 username 查找（兼容本地注册后被 Platform 绑定的场景）
            by_username = db.query(User).filter(User.username == username).first()
            if by_username and by_username.platform_user_id is None:
                # username 存在但未绑定 Platform → 绑定为当前 Platform 用户
                by_username.platform_user_id = platform_user_id
                by_username.email = user_info.get("email", by_username.email)
                by_username.is_admin = user_info.get("is_admin", False) or username == "admin"
                db.commit()
                db.refresh(by_username)
                user = by_username
                logger.info(f"Linked local user to Platform: {username} (platform_user_id={platform_user_id})")
            elif by_username and by_username.platform_user_id != platform_user_id:
                # username 被占用且属于不同的 Platform 账号 → 强制创建新用户，避免账户劫持
                logger.warning(
                    f"Username collision: {username} is owned by platform_user_id={by_username.platform_user_id}, "
                    f"but login attempt from platform_user_id={platform_user_id}. Creating separate account."
                )
                from utils.auth import hash_password
                dummy_password = hash_password(secrets.token_hex(32))
                is_admin = user_info.get("is_admin", False) or username == "admin"
                user = User(
                    username=f"{username}_{platform_user_id}",
                    platform_user_id=platform_user_id,
                    password_hash=dummy_password,
                    salt=None,
                    password_version=2,
                    is_admin=is_admin
                )
                db.add(user)
                db.commit()
                db.refresh(user)
                logger.info(f"Created new user from OAuth (username collision): {username}_{platform_user_id}")
            else:
                # 全新用户 → 创建
                from utils.auth import hash_password
                dummy_password = hash_password(secrets.token_hex(32))
                is_admin = user_info.get("is_admin", False) or username == "admin"
                user = User(
                    username=username,
                    platform_user_id=platform_user_id,
                    password_hash=dummy_password,
                    salt=None,
                    password_version=2,
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

    # P0-2 修复：token 只走 cookie，不暴露在 URL 中
    domain = f".{settings.FECLAW_PUBLIC_URL}" if settings.FECLAW_PUBLIC_URL else None
    response = RedirectResponse(url=redirect_to)

    response.set_cookie(
        key="feclaw_jwt",
        value=local_jwt,
        secure=True,
        samesite="lax",
        path="/",
        domain=domain,
        max_age=settings.JWT_EXPIRE_HOURS * 3600,
    )

    # P1-4 修复：保存 id_token 到 cookie，供 logout 时传递 id_token_hint
    if id_token:
        response.set_cookie(
            key="feclaw_id_token",
            value=id_token,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
            max_age=3600
        )

    # 保存 Platform access_token 到 cookie（非 HttpOnly），
    # 方便 Platform dashboard JS 跨域读取并调用 Platform API
    if access_token:
        # 从 FECLAW_PUBLIC_URL 推导 cookie domain（子域名部署需跨域共享 cookie）
        oauth_domain = settings.FECLAW_PUBLIC_URL or None
        response.set_cookie(
            key="platform_token",
            value=access_token,
            secure=True,
            samesite="lax",
            path="/",
            domain=oauth_domain,
            max_age=settings.JWT_EXPIRE_HOURS * 3600,
        )

    return response


# ────────────────────────────────────────────────────────────
# Mobile / API OAuth flow（P0-A-1 + P1-A-2 + P1-A-3）
# ────────────────────────────────────────────────────────────


class OAuthExchangeRequest(BaseModel):
    """Mobile 用 Platform access_token 换 FeClaw JWT pair（P0-A-1）"""
    platform_token: str = Field(..., description="Platform 登录后拿到的 access_token")
    # 可选：客户端拿到 id_token 时一并传，服务端可选择验签（此处暂不强制）
    id_token: Optional[str] = Field(default=None, description="可选 Platform id_token（OIDC）")


class OAuthRefreshRequest(BaseModel):
    """Mobile 用 FeClaw refresh_token 续 access_token（P1-A-2）"""
    refresh_token: str = Field(..., description="OAuth /exchange 返回的 refresh_token")


def _platform_base_url() -> str:
    """
    推导 Platform 内网 base（不走 CDN）。
    与 desktop_api._verify_platform_token 同源。
    """
    if settings.OAUTH_TOKEN_URL:
        return settings.OAUTH_TOKEN_URL.rsplit("/oauth/token", 1)[0].rstrip("/")
    return settings.OAUTH_PROVIDER_URL.rstrip("/")


async def _verify_platform_token_via_me(access_token: str) -> dict:
    """
    调 Platform `/api/auth/me` 验证 access_token，返回 user_info。
    与 desktop_api 行为一致：失败 401。
    """
    me_url = f"{_platform_base_url()}/api/auth/me"
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=certifi.where()) as client:
            resp = await client.get(
                me_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as e:
        logger.error(f"[oauth.exchange] Platform /api/auth/me unreachable: {e}")
        raise HTTPException(status_code=502, detail="Platform 认证服务暂时不可用，请稍后重试")

    if resp.status_code != 200:
        logger.warning(f"[oauth.exchange] Platform /api/auth/me returned {resp.status_code}")
        raise HTTPException(status_code=401, detail={
            "status": "invalid_platform_token",
            "message": "Invalid or expired Platform access_token",
        })

    data = resp.json()
    user_info = data.get("user", data)
    if not user_info or not user_info.get("id"):
        raise HTTPException(status_code=401, detail={
            "status": "invalid_platform_token",
            "message": "Platform token valid but no user info",
        })
    return user_info


@router.post("/exchange")
async def oauth_exchange(
    body: OAuthExchangeRequest,
    db: Session = Depends(get_db),
):
    """
    Mobile OAuth — 用 Platform access_token 换 FeClaw JWT pair（P0-A-1）。

    请求: { "platform_token": "<Platform access_token>" }
    响应: {
      "status": "success",
      "token": "<FeClaw access_token>",
      "refresh_token": "<FeClaw refresh_token>",
      "expires_in": <seconds>,
      "refresh_expires_in": <seconds>,
      "user_id": <int>,
      "username": <str>,
      "auth_method": "platform"
    }

    行为：
    1. 调 Platform /api/auth/me 验证 platform_token
    2. 按 platform_user_id 匹配/创建 FeClaw User（helper 抽自 desktop_api）
    3. 签发 access_token (HS256, type=access) + refresh_token (HS256, type=refresh)
    """
    user_info = await _verify_platform_token_via_me(body.platform_token)
    token_pair = issue_token_pair_for_platform_user(db, user_info)

    logger.info(
        f"[oauth.exchange] user_id={token_pair['user_id']} "
        f"username={token_pair['username']} auth_method=platform"
    )

    return JSONResponse(content={
        "status": "success",
        **token_pair,
    })


@router.post("/refresh")
async def oauth_refresh(body: OAuthRefreshRequest):
    """
    Mobile refresh — 用 refresh_token 换新 FeClaw access + refresh（P1-A-2）。

    与原占位（依赖 access token）不同：本端点接收 refresh_token body，
    验证 type=refresh 的 HS256 JWT，重新签发 access_token + 新 refresh_token。
    返回结构同 `/exchange`。
    """
    user_id = decode_refresh_token(body.refresh_token)
    if user_id is None:
        raise HTTPException(status_code=401, detail={
            "status": "invalid_refresh_token",
            "message": "refresh_token 无效、过期或类型不匹配",
        })

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.warning(f"[oauth.refresh] refresh_token 指向不存在的 user_id={user_id}")
            raise HTTPException(status_code=401, detail={
                "status": "invalid_refresh_token",
                "message": "refresh_token 指向的用户已不存在",
            })

        new_access, access_expires = sign_access_token(
            user_id=user.id,
            username=user.username,
            email=user.email,
            auth_method="platform",
        )
        new_refresh, refresh_expires = sign_refresh_token(user_id=user.id)
    finally:
        db.close()

    return JSONResponse(content={
        "status": "success",
        "token": new_access,
        "refresh_token": new_refresh,
        "expires_in": access_expires,
        "refresh_expires_in": refresh_expires,
        "user_id": user.id,
        "username": user.username,
        "auth_method": "platform",
    })


@router.get("/mobile-login")
async def oauth_mobile_login(
    request: Request,
    scheme: str = Query(default="feclaw", description="Mobile app 自定义 URL scheme（如 feclaw）"),
    state: str = Query(default="", description="Mobile 生成的 CSRF token（必填）"),
):
    """
    Mobile 入口：302 跳转到 Platform authorize，redirect_uri 用 `<scheme>://oauth/callback`（P1-A-3）。

    Mobile 端流程：
      1. App 启动 → 调 `GET /api/oauth/mobile-login?scheme=feclaw&state=<random>`
      2. 拿到 authorize URL → `Linking.openURL(url)`
      3. 在系统浏览器完成 Platform 登录
      4. Platform 302 → `feclaw://oauth/callback?code=...&state=...`
      5. Mobile 捕获 deep link → 用 code 调 Platform `/api/oauth/token` 拿 access_token
      6. 调 `POST /api/oauth/exchange` 拿 FeClaw token pair

    本端点也会把 state 写到 cookie（domain 设为 mobile 域），但因为 redirect_uri 是
    自定义 scheme，cookie 校验不可靠 —— **Mobile 必须自行在 /exchange 调用前用
    state 做 CSRF 校验**。此处 cookie 仅作为可选 debug / 兼容校验。

    校验：scheme 必须以字母开头且只含 [a-z0-9+.-]（RFC 3986 scheme 简化版），
    state 长度 ≥ 8。
    """
    import re
    if not re.match(r"^[a-z][a-z0-9+.\-]{1,63}$", scheme):
        raise HTTPException(status_code=400, detail={
            "status": "invalid_scheme",
            "message": "scheme 不合法（必须以字母开头，仅含 [a-z0-9+.-]，长度 2-64）",
        })
    if not state or len(state) < 8:
        raise HTTPException(status_code=400, detail={
            "status": "invalid_state",
            "message": "state 必填且长度 ≥ 8（防 CSRF）",
        })

    # 平台必须支持 mobile custom scheme 回调，否则 authorize 时 Platform 会拒绝
    redirect_uri = f"{scheme}://oauth/callback"

    if not settings.OAUTH_PROVIDER_URL or not settings.OAUTH_CLIENT_ID:
        logger.warning("[oauth.mobile-login] OAuth 未配置")
        raise HTTPException(status_code=503, detail={
            "status": "oauth_not_configured",
            "message": "OAuth provider 未配置，请联系管理员",
        })

    # 与 oauth_service.get_authorize_url 一致，但 redirect_uri 用 mobile scheme
    base_authorize = settings.OAUTH_AUTHORIZE_URL or (
        settings.OAUTH_PROVIDER_URL.rstrip("/") + "/authorize"
    )
    params = {
        "response_type": "code",
        "client_id": settings.OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": "openid profile email",
    }
    authorize_url = f"{base_authorize}?{urlencode(params)}"

    logger.info(
        f"[oauth.mobile-login] 302 -> Platform authorize (scheme={scheme}, state={state[:8]}...)"
    )

    # 可选：在 cookie 里写一份 state（best-effort，对 mobile scheme 不保证可用）
    response = RedirectResponse(url=authorize_url, status_code=302)
    cookie_name = f"oauth_state_{state[:16]}"
    response.set_cookie(
        key=cookie_name,
        value=state,
        max_age=STATE_COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="none",
    )
    return response


@router.post("/logout")
async def oauth_logout(request: Request):
    """
    OAuth 注销 - 清除本地 cookie 并返回 Platform end_session 跳转地址
    """
    from services.oauth_service import OAuthService

    # P1-4 修复：传递 id_token_hint，让 Platform 端正确完成 RP-Initiated Logout
    id_token_hint = request.cookies.get("feclaw_id_token", "")
    oauth_svc = OAuthService()
    response = JSONResponse(content={
        "status": "success",
        "message": "Logged out successfully",
        "redirect_url": oauth_svc.build_logout_url(
            id_token=id_token_hint,
            post_logout_redirect_uri=f"https://{settings.FECLAW_PUBLIC_URL}/login" if settings.FECLAW_PUBLIC_URL else None
        )
    })

    # 清除 FeClaw 自身 cookie
    response.delete_cookie(key="feclaw_jwt", path="/")
    response.delete_cookie(key="feclaw_id_token", path="/")

    return response


@router.get("/logout")
async def oauth_logout_get(
    request: Request,
    redirect: str = Query(default="", description="退出后跳转地址")
):
    """
    OAuth 注销（GET）— 用于跨域退出跳转

    Platform 退出时会重定向到这个地址，FeClaw 清掉自己的 cookie 后跳回。
    """
    # 检查 redirect 是否在白名单中
    safe_redirect = "/login"
    if redirect:
        app_url = f"http://{settings.FECLAW_PUBLIC_URL}" if settings.FECLAW_PUBLIC_URL else "http://localhost:8080"
        allowed_prefixes = [app_url]
        if any(redirect.startswith(p) for p in allowed_prefixes):
            safe_redirect = redirect

    response = RedirectResponse(url=safe_redirect, status_code=302)
    response.delete_cookie(key="feclaw_jwt", path="/")
    response.delete_cookie(key="feclaw_id_token", path="/")
    return response


@router.get("/me")
async def oauth_me(
    user: User = Depends(get_current_user),
):
    """
    获取当前登录用户信息
    """
    return JSONResponse(content={
        "status": "success",
        "user": {
            "id": user.id,
            "username": user.username,
            "is_admin": user.is_admin,
            "created_at": user.created_at.isoformat() if user.created_at else None
        }
    })