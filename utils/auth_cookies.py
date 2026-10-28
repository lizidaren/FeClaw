"""
认证 Cookie 统一读写（FeClaw 全局 HS256 JWT 及其配套 cookie）

单一事实来源：**cookie 名 + domain 推导**。

登录写入（`routers/oauth.py` callback）与登出清除
（`routers/feclaw_domain.py`、`routers/oauth.py` logout）**必须**共用这里的函数 ——
否则 domain/path 不匹配会导致「登出假成功、cookie 删不掉」。

domain 规则：
    FECLAW_PUBLIC_URL = "feclaw.lizidaren.cn" → ".feclaw.lizidaren.cn"
        （前导点 ⇒ 根域名 + 所有子域名共享，SSO 必需）
    未配置（本地开发）→ None ⇒ host-only cookie
"""
from typing import Iterable, List, Optional

from config import settings

COOKIE_PATH = "/"

# 跨子域名共享的认证 cookie（登出时必清，名字与写入侧严格一致）
AUTH_COOKIE_JWT = "feclaw_jwt"
AUTH_COOKIE_ID_TOKEN = "feclaw_id_token"   # 注意：不是 "id_token"
AUTH_COOKIE_PLATFORM_TOKEN = "platform_token"
AUTH_COOKIE_NAMES = (AUTH_COOKIE_JWT, AUTH_COOKIE_ID_TOKEN, AUTH_COOKIE_PLATFORM_TOKEN)

# Agent 作用域 TOTP cookie 的名字前缀：写侧是 f"feclaw_jwt_totp_{agent_hash}"
TOTP_COOKIE_PREFIX = "feclaw_jwt_totp_"


def auth_cookie_domain() -> Optional[str]:
    """推导认证 cookie 的 domain（从 FECLAW_PUBLIC_URL）。

    与是否带端口/协议无关；返回带前导点的根域名，未配置则返回 None。
    """
    raw = (getattr(settings, "FECLAW_PUBLIC_URL", "") or "").strip()
    if not raw:
        return None
    # 容忍 "https://host:port/path" 形式
    host = raw.split("://")[-1].split("/")[0].split(":")[0].strip(".")
    if not host:
        return None
    return f".{host}"


def set_auth_cookie(
    response,
    key: str,
    value: str,
    *,
    max_age: Optional[int] = None,
    httponly: bool = False,
    secure: bool = True,
    samesite: str = "lax",
):
    """写入认证 cookie（domain 由 :func:`auth_cookie_domain` 统一推导）。

    与 :func:`clear_auth_cookies` 对称 —— 写入与清除用同一个 domain，
    这是「登出真的能删掉 cookie」的前提。
    """
    response.set_cookie(
        key=key,
        value=value,
        max_age=max_age,
        httponly=httponly,
        secure=secure,
        samesite=samesite,
        path=COOKIE_PATH,
        domain=auth_cookie_domain(),
    )
    return response


def clear_auth_cookies(response, names: Iterable[str] = AUTH_COOKIE_NAMES):
    """清除全部认证 cookie（登出唯一入口）。

    每个名字清两次：

    1. **带 domain** —— 覆盖 `routers/oauth.py` 写入的跨子域名 cookie；
    2. **不带 domain** —— 覆盖 `routers/user.py` 本地登录 / 前端 JS 写入的 host-only cookie。

    两次不是冗余：cookie 身份 = name + domain + path（RFC 6265），
    host-only cookie 与 domain cookie 是**两个不同的 cookie**，必须各清一次。

    故意不带 ``Secure`` 属性：在 HTTPS 下删 Secure cookie 不需要该标志，
    而在 HTTP 开发环境带上它会让浏览器整条拒绝 ⇒ 反而删不掉。
    """
    domain = auth_cookie_domain()
    for name in names:
        if domain:
            response.delete_cookie(key=name, path=COOKIE_PATH, domain=domain)
        response.delete_cookie(key=name, path=COOKIE_PATH)
    return response


# ────────────────────────────────────────────────────────────────────
# T18a：会话 cookie 的「写」与「清全部变体」单点
# ────────────────────────────────────────────────────────────────────

def cookie_secure_for_request(request) -> bool:
    """按请求决定 cookie 是否带 ``Secure``（FIX-F G7 口径，单点实现）。

    - HTTPS 请求或 `X-Forwarded-Proto: https` → **永远** Secure；
    - `COOKIE_SECURE` 显式为 True/False 时，True 强制 Secure；
      False 只对**非 HTTPS** 生效（不能把生产 HTTPS 会话降级为明文）。
    """
    is_https = False
    try:
        is_https = str(request.url.scheme) == "https" or (
            (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https"
        )
    except Exception:  # pragma: no cover - 非常规 request 对象
        is_https = False

    configured = getattr(settings, "COOKIE_SECURE", None)
    if configured is not None:
        return bool(configured) or is_https
    return is_https


def set_session_cookie(response, token: str, request=None, *, max_age: Optional[int] = None):
    """写入**浏览器会话** cookie（T18a：服务端 HttpOnly，前端不再存 JWT）。

    - `httponly=True`：JS 读不到 ⇒ XSS 偷不到会话令牌；
    - `samesite="lax"`：跨站 POST 不携带（CSRF 的第二道补充，不是唯一防线）；
    - domain 走 :func:`auth_cookie_domain`（与清除共用同一推导，保证删得掉）。

    `max_age` 缺省 = `JWT_EXPIRE_HOURS`；与 token 的 `exp` 一致。
    """
    if max_age is None:
        try:
            max_age = int(getattr(settings, "JWT_EXPIRE_HOURS", 168)) * 3600
        except Exception:
            max_age = 7 * 24 * 3600
    secure = cookie_secure_for_request(request) if request is not None else True
    return set_auth_cookie(
        response, AUTH_COOKIE_JWT, token,
        max_age=max_age, httponly=True, secure=secure, samesite="lax",
    )


def clear_session_cookies(response, request=None, totp_agent_hashes: Optional[Iterable[str]] = None):
    """清除**全部**会话相关 cookie 变体（T18a「清 cookie 覆盖 host/domain 变体」）。

    比 :func:`clear_auth_cookies` 多做三件事：

    1. 额外清除 Agent 作用域 TOTP cookie（`feclaw_jwt_totp_<hash>`）：
       名字来自 `totp_agent_hashes`（显式）与**当前请求 cookie 里的实际前缀**
       （`request` 存在时），domain + host-only 各清一次；
    2. 清除 CSRF cookie（否则登出后旧 token 还能配新会话）；
    3. 保持与写入侧同一 domain 推导，host-only 变体也清（老前端 JS 写的）。
    """
    clear_auth_cookies(response)

    names: List[str] = []
    for agent_hash in (totp_agent_hashes or ()):
        if agent_hash:
            names.append(f"{TOTP_COOKIE_PREFIX}{agent_hash}")
    if request is not None:
        try:
            for cookie_name in request.cookies.keys():
                if cookie_name.startswith(TOTP_COOKIE_PREFIX) and cookie_name not in names:
                    names.append(cookie_name)
        except Exception:  # pragma: no cover
            pass
    if names:
        clear_auth_cookies(response, names=tuple(names))

    try:
        from utils.csrf import clear_csrf_cookie
        clear_csrf_cookie(response)
    except Exception:  # pragma: no cover - csrf 模块不可用时不阻塞登出
        pass
    return response


__all__ = [
    "COOKIE_PATH",
    "AUTH_COOKIE_JWT",
    "AUTH_COOKIE_ID_TOKEN",
    "AUTH_COOKIE_PLATFORM_TOKEN",
    "AUTH_COOKIE_NAMES",
    "TOTP_COOKIE_PREFIX",
    "auth_cookie_domain",
    "set_auth_cookie",
    "clear_auth_cookies",
    "cookie_secure_for_request",
    "set_session_cookie",
    "clear_session_cookies",
]
