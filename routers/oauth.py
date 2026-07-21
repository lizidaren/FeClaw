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
from models.database import get_db, SessionLocal, User, UserLink
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

        # 安全匹配：先按 UserLink(provider=platform) 精准查（P0-1 修复：禁止 or_ 条件）
        existing_link = db.query(UserLink).filter(
            UserLink.provider == "platform",
            UserLink.provider_user_id == platform_user_id,
        ).first()
        existing = db.query(User).filter(User.id == existing_link.user_id).first() if existing_link else None

        if existing:
            # 按 platform_user_id 精准匹配 -> 更新
            existing.email = user_info.get("email", existing.email)
            existing.is_admin = user_info.get("is_admin", False) or username == "admin"
            if existing_link:
                existing_link.provider_username = username
            db.commit()
            db.refresh(existing)
            user = existing
            logger.info(f"Updated existing user from OAuth: {username}")
        else:
            # 按 username 查找（兼容本地注册后被 Platform 绑定的场景）
            by_username = db.query(User).filter(User.username == username).first()
            existing_links_count = (
                db.query(UserLink).filter(UserLink.user_id == by_username.id).count()
                if by_username else 0
            )
            if by_username and existing_links_count == 0:
                # username 存在但无任何外部绑定 -> 绑定为当前 Platform 用户
                link = UserLink(
                    user_id=by_username.id,
                    provider="platform",
                    provider_user_id=platform_user_id,
                    provider_username=username,
                )
                db.add(link)
                by_username.email = user_info.get("email", by_username.email)
                by_username.is_admin = user_info.get("is_admin", False) or username == "admin"
                db.commit()
                db.refresh(by_username)
                user = by_username
                logger.info(f"Linked local user to Platform: {username} (platform_user_id={platform_user_id})")
            elif by_username and existing_links_count > 0:
                # username 被占用且已绑其他 Provider 账号 -> 强制创建新用户，避免账户劫持
                logger.warning(
                    f"Username collision: {username} is already linked to other provider(s), "
                    f"but login attempt from platform_user_id={platform_user_id}. Creating separate account."
                )
                from utils.auth import hash_password
                dummy_password = hash_password(secrets.token_hex(32))
                is_admin = user_info.get("is_admin", False) or username == "admin"
                user = User(
                    username=f"{username}_{platform_user_id}",
                    password_hash=dummy_password,
                    salt=None,
                    password_version=2,
                    is_admin=is_admin
                )
                db.add(user)
                db.flush()
                link = UserLink(
                    user_id=user.id,
                    provider="platform",
                    provider_user_id=platform_user_id,
                    provider_username=username,
                )
                db.add(link)
                db.commit()
                db.refresh(user)
                logger.info(f"Created new user from OAuth (username collision): {username}_{platform_user_id}")
            else:
                # 全新用户 -> 创建
                from utils.auth import hash_password
                dummy_password = hash_password(secrets.token_hex(32))
                is_admin = user_info.get("is_admin", False) or username == "admin"
                user = User(
                    username=username,
                    password_hash=dummy_password,
                    salt=None,
                    password_version=2,
                    is_admin=is_admin
                )
                db.add(user)
                db.flush()
                link = UserLink(
                    user_id=user.id,
                    provider="platform",
                    provider_user_id=platform_user_id,
                    provider_username=username,
                )
                db.add(link)
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
    """
    Mobile OAuth exchange request — supports two flows:

    1. **Legacy (P0-A-1)** — mobile already exchanged the code against Platform
       and got a Platform access_token. Just send ``platform_token`` and FeClaw
       verifies it via ``Platform /api/auth/me`` and issues FeClaw JWT pair.

    2. **PKCE (FeClaw-Mobile)** — mobile generated a ``code_verifier`` and
       sent the ``code`` straight to FeClaw. FeClaw relays both to Platform
       ``/token`` (no client_secret — PKCE replaces shared-secret trust),
       then fetches userinfo and issues FeClaw JWT pair.

    All three fields are optional individually but **at least one of the two
    flows must be present**. ``extra="forbid"`` is intentionally NOT set so
    the mobile client can also forward ``redirect_uri`` (Platform ignores
    unknown fields on the token endpoint when it accepts them).
    """
    # Legacy flow
    platform_token: Optional[str] = Field(
        default=None,
        description="Platform access_token（legacy 流程，mobile 已自行换过 code）",
    )
    id_token: Optional[str] = Field(
        default=None,
        description="可选 Platform id_token（OIDC）",
    )
    # PKCE flow（FeClaw-Mobile 主流）
    code: Optional[str] = Field(
        default=None,
        description="Platform 授权码（PKCE 流程）",
    )
    code_verifier: Optional[str] = Field(
        default=None,
        description="PKCE code_verifier（RFC 7636）",
    )
    redirect_uri: Optional[str] = Field(
        default=None,
        description="可选：mobile callback URI（与 /authorize 时一致）",
    )


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
    body: Optional[OAuthExchangeRequest] = None,
    db: Session = Depends(get_db),
):
    """
    Mobile OAuth — exchange Platform credentials for FeClaw JWT pair.

    Two flows supported (auto-detected by request body):

    **PKCE flow (FeClaw-Mobile):**
      请求: { "code": "...", "code_verifier": "..." }
      流程: POST Platform /token (code + code_verifier) → Platform /userinfo →
            find/create FeClaw User → issue FeClaw access + refresh

    **Legacy flow (P0-A-1):**
      请求: { "platform_token": "<Platform access_token>" }
      流程: Platform /api/auth/me → find/create FeClaw User → issue FeClaw
            access + refresh

    响应（两种流程一致）:
      {
        "status": "success",
        "token": "<FeClaw access_token>",
        "refresh_token": "<FeClaw refresh_token>",
        "expires_in": <seconds>,
        "refresh_expires_in": <seconds>,
        "user_id": <int>,
        "username": <str>,
        "auth_method": "platform"
      }

    Body 全空 → 400；body 完全缺失（None）同样 400（不返回 422）。
    """
    if body is None:
        raise HTTPException(status_code=400, detail={
            "status": "invalid_request",
            "message": "either code+code_verifier (PKCE) or platform_token (legacy) required",
        })

    # ────────────────────────────────────────────────────────────
    # 输入校验：两种流程必须二选一
    # ────────────────────────────────────────────────────────────
    # 半截 PKCE 请求（只给了 code 或 verifier 之一）— 先报更具体的错
    if (body.code and not body.code_verifier) or (body.code_verifier and not body.code):
        raise HTTPException(status_code=400, detail={
            "status": "invalid_request",
            "message": "code and code_verifier must be provided together (PKCE)",
        })

    has_pkce = bool(body.code and body.code_verifier)
    has_legacy = bool(body.platform_token)

    if not has_pkce and not has_legacy:
        raise HTTPException(status_code=400, detail={
            "status": "invalid_request",
            "message": "either code+code_verifier (PKCE) or platform_token (legacy) required",
        })

    # ────────────────────────────────────────────────────────────
    # 流程 A：PKCE（FeClaw-Mobile）
    # ────────────────────────────────────────────────────────────
    if has_pkce:
        # 使用 verbose 版本，区分 invalid_grant（4xx）和网络错误（5xx / 0）
        pkce_result = await oauth_service.exchange_code_with_pkce_verbose(
            code=body.code,
            code_verifier=body.code_verifier,
            redirect_uri=body.redirect_uri,
        )
        if not pkce_result.get("ok"):
            status = pkce_result.get("status", 0)
            error_code = pkce_result.get("error", "unknown")
            error_desc = pkce_result.get("error_description", "")
            # 4xx（如 invalid_grant / invalid_request）→ 502 给客户端，
            # 让上层明确知道是 Platform 拒绝了 code，而不是 FeClaw 自身故障
            logger.warning(
                f"[oauth.exchange.pkce] Platform rejected code: "
                f"status={status} error={error_code} description={error_desc}"
            )
            raise HTTPException(
                status_code=502,
                detail={
                    "status": "platform_token_exchange_failed",
                    "error": error_code,
                    "message": error_desc or "Platform rejected the authorization code",
                    "upstream_status": status,
                },
            )

        token_data = pkce_result["data"]
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error("[oauth.exchange.pkce] Platform /token returned no access_token")
            raise HTTPException(status_code=502, detail={
                "status": "platform_token_exchange_failed",
                "message": "Platform /token response missing access_token",
            })

        # 拿 userinfo（复用现有 helper，与 web callback 一致）
        user_info = await oauth_service.get_userinfo(access_token)
        if not user_info:
            raise HTTPException(status_code=502, detail={
                "status": "platform_userinfo_failed",
                "message": "Platform userinfo endpoint unreachable",
            })

        # 复用一站式 helper：platform user_info -> FeClaw access + refresh
        # 这一步会 match/create FeClaw User，并签发 HS256 token pair。
        token_pair = issue_token_pair_for_platform_user(db, user_info)
        logger.info(
            f"[oauth.exchange.pkce] user_id={token_pair['user_id']} "
            f"username={token_pair['username']} auth_method=platform"
        )
        return JSONResponse(content={
            "status": "success",
            **token_pair,
        })

    # ────────────────────────────────────────────────────────────
    # 流程 B：Legacy platform_token
    # ────────────────────────────────────────────────────────────
    user_info = await _verify_platform_token_via_me(body.platform_token)
    token_pair = issue_token_pair_for_platform_user(db, user_info)

    logger.info(
        f"[oauth.exchange.legacy] user_id={token_pair['user_id']} "
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
    code_challenge: str = Query(default="", description="PKCE code_challenge（FeClaw-Mobile 推荐）"),
    code_challenge_method: str = Query(default="S256", description="PKCE method（默认 S256，可选 plain）"),
):
    """
    Mobile 入口：302 跳转到 Platform authorize，redirect_uri 用 `<scheme>://oauth/callback`（P1-A-3）。

    Mobile 端流程（PKCE 推荐，FeClaw-Mobile 默认走此路径）：
      1. App 启动 → 生成 code_verifier + code_challenge
         → 调 `GET /api/oauth/mobile-login?scheme=feclaw&state=<random>&code_challenge=<...>&code_challenge_method=S256`
      2. 拿到 authorize URL → `Linking.openURL(url)`
      3. 在系统浏览器完成 Platform 登录
      4. Platform 302 → `feclaw://oauth/callback?code=...&state=...`
      5. Mobile 捕获 deep link → 调 `POST /api/oauth/exchange`
         body = { code, code_verifier, redirect_uri: "<scheme>://oauth/callback" }
      6. FeClaw 用 code + code_verifier 换 Platform token → 拿 userinfo → 签 FeClaw JWT pair

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

    # PKCE 参数校验：method 必须是 S256 或 plain；challenge 必须 base64url 安全字符
    if code_challenge:
        if code_challenge_method not in ("S256", "plain"):
            raise HTTPException(status_code=400, detail={
                "status": "invalid_pkce",
                "message": "code_challenge_method 必须是 'S256' 或 'plain'",
            })
        if not re.match(r"^[A-Za-z0-9_\-]{43,128}$", code_challenge):
            raise HTTPException(status_code=400, detail={
                "status": "invalid_pkce",
                "message": "code_challenge 必须是 43-128 个字符的 [A-Za-z0-9_-] 串",
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
    # 转发 PKCE 参数（如有）— Platform 会把它们存进 auth code 上下文，
    # 等 FeClaw 后续 POST /token 时校验 code_verifier 与之匹配。
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = code_challenge_method

    authorize_url = f"{base_authorize}?{urlencode(params)}"

    logger.info(
        f"[oauth.mobile-login] 302 -> Platform authorize (scheme={scheme}, state={state[:8]}..., "
        f"pkce={'yes' if code_challenge else 'no'})"
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