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
from typing import Iterable, Optional

from config import settings

COOKIE_PATH = "/"

# 跨子域名共享的认证 cookie（登出时必清，名字与写入侧严格一致）
AUTH_COOKIE_JWT = "feclaw_jwt"
AUTH_COOKIE_ID_TOKEN = "feclaw_id_token"   # 注意：不是 "id_token"
AUTH_COOKIE_PLATFORM_TOKEN = "platform_token"
AUTH_COOKIE_NAMES = (AUTH_COOKIE_JWT, AUTH_COOKIE_ID_TOKEN, AUTH_COOKIE_PLATFORM_TOKEN)


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
