"""
CSRF / Origin 防护（T18a 唯一实现）

两条防线，**同一个模块**，不要在各 router 里手写第二套：

1. **Origin / Referer 校验** —— 浏览器对跨站 `POST/PUT/PATCH/DELETE` 一定带
   `Origin`；带 `Origin` 就必须落在允许列表内（同源 or 配置的来源）。
   这是「跨站 mutation 拒」的主防线，SameSite 只是补充（spec T18 明确
   「SameSite 不作为唯一防线」）。
2. **双提交 token（double submit）** —— 服务端下发一个**可读** cookie
   `feclaw_csrf`，前端在请求头 `X-CSRF-Token` 回传；服务端常量时间比较两者。
   cookie 由浏览器自动携带，攻击者页面能触发请求但**读不到** cookie 值
   （跨源读 cookie 需要 CORS 授权，主应用不给），因此无法伪造请求头。

适用边界（`csrf_protect` 的判定顺序）：

- 有 `Authorization: Bearer` → **跳过 token 校验**：Bearer 是非浏览器的显式
  凭据，浏览器不会自动携带，天然免疫 CSRF（移动端 / 桌面端保持可用）。
  仍然做 Origin 校验（如果带了 Origin）。
- 安全方法（GET/HEAD/OPTIONS/TRACE）→ 放行。
- 没有任何会话 cookie（未登录的 login/register）→ 只做 Origin 校验。
  （登录 CSRF 也靠 Origin 挡住：浏览器跨站 POST 一定带 Origin。）
- 其余（cookie 认证的 mutation）→ 必须同时通过 Origin 校验 + token 校验。

**故意不在这里做的事**：不注册中间件、不改 `main.py`。需要全站挂载时由
`main.py` 的 owner 在获得「跨界请求」后统一挂；本模块提供 `csrf_protect`
依赖与 `check_request_origin` 函数供两条路复用。
"""

import hmac
import logging
import secrets
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response, status

from config import settings

logger = logging.getLogger(__name__)

# 不修改服务端状态的方法（RFC 7231 §4.2.1 + Fetch 的简单请求集合）
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# 会话 cookie 名（与 utils.auth_cookies 一致；此处只读，不写入）
_SESSION_COOKIE_NAMES = ("feclaw_jwt", "platform_session")
_TOTP_COOKIE_PREFIX = "feclaw_jwt_totp_"

# 统一失败响应体（不泄漏内部判定细节）
_CSRF_FAIL_DETAIL = {"status": "csrf_failed", "message": "CSRF 校验失败，请刷新页面后重试"}
_ORIGIN_FAIL_DETAIL = {"status": "origin_rejected", "message": "请求来源不被允许"}


# ────────────────────────────────────────────────────────────────────
# 配置读取（全部从 settings 读，避免常量散落）
# ────────────────────────────────────────────────────────────────────

def csrf_cookie_name() -> str:
    return getattr(settings, "CSRF_COOKIE_NAME", "feclaw_csrf") or "feclaw_csrf"


def csrf_header_name() -> str:
    return getattr(settings, "CSRF_HEADER_NAME", "X-CSRF-Token") or "X-CSRF-Token"


def csrf_enabled() -> bool:
    return bool(getattr(settings, "CSRF_ENABLED", True))


# ────────────────────────────────────────────────────────────────────
# Origin 规范化与允许列表
# ────────────────────────────────────────────────────────────────────

def _strip_default_port(scheme: str, host: str, port: Optional[int]) -> str:
    if port is None:
        return host
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return host
    return f"{host}:{port}"


def normalize_origin(value: Optional[str]) -> Optional[str]:
    """把 `Origin` / `Referer` / 配置里的 URL 规范成 `scheme://host[:port]`。

    返回 None 表示**不可用**（空、`null`、非法、非 http(s) 协议）。
    `javascript:` / `data:` / `file:` 等一律 None —— 它们不是合法来源。
    """
    if not value:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() == "null":
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return f"{scheme}://{_strip_default_port(scheme, host, port)}"


def _public_url_origins() -> List[str]:
    """`FECLAW_PUBLIC_URL` 推导的来源（容忍 host / host:port / 带协议形式）。"""
    raw = (getattr(settings, "FECLAW_PUBLIC_URL", "") or "").strip()
    if not raw:
        return []
    candidates = []
    if "://" in raw:
        candidates.append(raw)
    else:
        candidates.append(f"https://{raw}")
        candidates.append(f"http://{raw}")
    out = []
    for c in candidates:
        norm = normalize_origin(c)
        if norm and norm not in out:
            out.append(norm)
    return out


def _configured_extra_origins(attr: str) -> List[str]:
    raw = getattr(settings, attr, []) or []
    if isinstance(raw, str):
        items: Iterable[str] = [p for p in raw.split(",") if p.strip()]
    else:
        items = raw
    out: List[str] = []
    for item in items:
        norm = normalize_origin(item)
        if norm and norm not in out:
            out.append(norm)
    return out


def _request_self_origins(request: Request) -> List[str]:
    """请求自身的来源候选（同源判定用）。

    直接比较 `Origin` 与请求的 Host：浏览器设的 Host 就是真实目标，
    跨站请求的 Origin 一定与之不同 ⇒ 不相等即拒。

    ⚠️ 同时接受该 Host 的 http/https 两种 scheme：TLS 常在反代/CLB 终止，
    而 `request.url.scheme` 在没配 `X-Forwarded-Proto` 时是 http —— 只认一种
    scheme 会把**正常同源**的 mutation 全部误拒（生产事故级）。比较的仍然是
    精确 host，攻击者域不因此获得任何放行。
    """
    out: List[str] = []
    host = (request.headers.get("host") or "").strip()
    if host:
        for scheme in ("http", "https"):
            norm = normalize_origin(f"{scheme}://{host}")
            if norm and norm not in out:
                out.append(norm)

    # CDN / 反代场景：X-Forwarded-Proto + X-Forwarded-Host。
    # 只接受与 FECLAW_PUBLIC_URL 同域（含子域）的转发 host，防头注入。
    fwd_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    fwd_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if fwd_host:
        bare = fwd_host.split(":")[0].lower()
        public_host = ""
        public_raw = (getattr(settings, "FECLAW_PUBLIC_URL", "") or "").split("://")[-1]
        if public_raw:
            public_host = public_raw.split("/")[0].split(":")[0].lower()
        trusted = (
            (public_host and (bare == public_host or bare.endswith("." + public_host)))
            or (not public_host and bare in ("localhost", "127.0.0.1", "[::1]", "::1"))
        )
        if trusted:
            schemes = ("http", "https") if fwd_proto not in ("http", "https") else ("https", "http")
            for scheme in schemes:
                norm = normalize_origin(f"{scheme}://{fwd_host}")
                if norm and norm not in out:
                    out.append(norm)
    return out


def allowed_origins(request: Request) -> List[str]:
    """本次请求允许的 Origin 全集（同源 + 配置来源 + 开发本地地址）。"""
    out: List[str] = []
    for origin in _request_self_origins(request) + _public_url_origins() + _configured_extra_origins("CSRF_EXTRA_ORIGINS"):
        if origin not in out:
            out.append(origin)
    # 未配置公网域名时（本地开发）额外接受 localhost / 127.0.0.1
    if not getattr(settings, "FECLAW_PUBLIC_URL", ""):
        for host in ("localhost", "127.0.0.1", "[::1]", "::1"):
            for scheme in ("http", "https"):
                norm = normalize_origin(f"{scheme}://{host}")
                if norm and norm not in out:
                    out.append(norm)
    return out


def origin_allowed(origin: Optional[str], allowed: Iterable[str]) -> bool:
    """`origin` 是否在 `allowed` 内（规范化后精确匹配，不做前缀/后缀猜测）。"""
    norm = normalize_origin(origin)
    if norm is None:
        return False
    return norm in set(allowed)


def check_request_origin(request: Request) -> Tuple[bool, str]:
    """校验 `Origin`（优先）或 `Referer`。

    返回 `(ok, reason)`：

    - `ok=True, reason="origin"`      —— Origin 命中允许列表
    - `ok=True, reason="referer"`     —— 无 Origin，Referer 命中
    - `ok=True, reason="no_origin"`   —— 两者皆无（非浏览器客户端）
    - `ok=False, reason="bad_origin"` —— Origin 非法/不在列表（含 `null`）
    - `ok=False, reason="bad_referer"`—— Referer 非法/不在列表
    - `ok=False, reason="origin_required"` —— 配置要求 Origin 但缺失
    """
    if not csrf_enabled():
        return True, "disabled"

    origin_header = request.headers.get("origin")
    referer_header = request.headers.get("referer")
    allowed = allowed_origins(request)

    if origin_header is None and referer_header is None:
        if getattr(settings, "CSRF_REQUIRE_ORIGIN", False):
            return False, "origin_required"
        return True, "no_origin"

    if origin_header is not None:
        if origin_allowed(origin_header, allowed):
            return True, "origin"
        return False, "bad_origin"

    if origin_allowed(referer_header, allowed):
        return True, "referer"
    return False, "bad_referer"


# ────────────────────────────────────────────────────────────────────
# 双提交 token
# ────────────────────────────────────────────────────────────────────

def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _cookie_token(request: Request) -> Optional[str]:
    try:
        value = request.cookies.get(csrf_cookie_name())
    except Exception:  # pragma: no cover - 极端解析错误
        return None
    return value or None


def _header_token(request: Request) -> Optional[str]:
    # Starlette 的 headers 大小写不敏感
    value = request.headers.get(csrf_header_name())
    if value is None:
        # 兜底：某些代理会改大小写，再按常见别名取一次
        value = request.headers.get("x-csrf-token")
    return (value or "").strip() or None


def csrf_token_matches(request: Request) -> bool:
    """双提交校验：cookie 与请求头都存在、非空且常量时间相等。"""
    cookie_token = _cookie_token(request)
    header_token = _header_token(request)
    if not cookie_token or not header_token:
        return False
    # 长度先比（hmac.compare_digest 对 bytes 才常量时间，str 需同 ASCII 编码）
    try:
        a = cookie_token.encode("utf-8")
        b = header_token.encode("utf-8")
    except Exception:  # pragma: no cover
        return False
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


def set_csrf_cookie(response: Response, token: str, request: Optional[Request] = None) -> Response:
    """把**已生成**的 CSRF token 写进可读 cookie（与响应体里的值必须一致）。"""
    secure = True
    if request is not None:
        from utils.auth_cookies import cookie_secure_for_request
        secure = cookie_secure_for_request(request)
    response.set_cookie(
        key=csrf_cookie_name(),
        value=token,
        max_age=86400,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )
    return response


def issue_csrf_token(response: Response, request: Optional[Request] = None) -> str:
    """下发（可读）CSRF cookie，返回 token（调用方可再放进响应体）。

    cookie 必须**可被 JS 读取**（httponly=False），否则双提交无法回传；
    它本身不是凭据 —— 泄露它只能配合受害者的会话 cookie 才有效，
    而 CORS 不允许跨源读 cookie。
    """
    token = generate_csrf_token()
    set_csrf_cookie(response, token, request)
    return token



def clear_csrf_cookie(response: Response) -> Response:
    """清除 CSRF cookie（登出时调用；host-only 与常见 domain 变体各清一次）。"""
    from utils.auth_cookies import auth_cookie_domain

    domain = auth_cookie_domain()
    if domain:
        response.delete_cookie(key=csrf_cookie_name(), path="/", domain=domain)
    response.delete_cookie(key=csrf_cookie_name(), path="/")
    return response


# ────────────────────────────────────────────────────────────────────
# 供路由使用的判定与依赖
# ────────────────────────────────────────────────────────────────────

def is_bearer_authenticated(request: Request) -> bool:
    """请求是否带显式 Bearer 凭据（非浏览器令牌，免疫 CSRF）。"""
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    return auth_header.startswith("Bearer ") and len(auth_header) > 7


def has_session_cookie(request: Request) -> bool:
    """请求是否带**会话类** cookie（含 Agent 作用域 TOTP cookie）。"""
    try:
        cookies = request.cookies
    except Exception:  # pragma: no cover
        return False
    for name in _SESSION_COOKIE_NAMES:
        if cookies.get(name):
            return True
    # TOTP cookie 名带 agent_hash，只能前缀扫描
    for name in cookies.keys():
        if name.startswith(_TOTP_COOKIE_PREFIX) and cookies.get(name):
            return True
    return False


def requires_csrf_token(request: Request) -> bool:
    """本次请求是否**必须**带双提交 token。

    - 只针对不安全方法
    - Bearer 认证排除（显式令牌）
    - 未带会话 cookie 排除（login / register 等登录前流程，靠 Origin 挡住登录 CSRF）
    """
    if request.method.upper() in SAFE_METHODS:
        return False
    if is_bearer_authenticated(request):
        return False
    return has_session_cookie(request)


async def csrf_protect_origin_only(request: Request) -> None:
    """FastAPI 依赖：**只做 Origin/Referer 校验**（不要求双提交 token）。

    用于「凭据建立」类端点（`/api/user/login`、`/api/user/register`）：

    - 这类请求**不是**用现有会话 cookie 认证的 —— 它们的目的就是拿到新会话，
      所以旧 cookie 是否附带 CSRF token 与本次操作无关；
      若强求 token，就会出现「刚注册完（已有会话 cookie）→ 再注册/换号注册」
      这类请求被无理由 403 的坑。
    - 登录 CSRF（把受害者登进攻击者账号）靠 **Origin 校验** 挡住：浏览器跨站
      POST 一定带攻击者域名的 `Origin`。
    """
    if not csrf_enabled():
        return
    ok, reason = check_request_origin(request)
    if not ok:
        logger.warning(
            "[CSRF] origin rejected (origin-only): path=%s method=%s reason=%s origin=%r referer=%r",
            request.url.path, request.method, reason,
            (request.headers.get("origin") or "")[:200],
            (request.headers.get("referer") or "")[:200],
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_ORIGIN_FAIL_DETAIL)


async def csrf_protect(request: Request) -> None:
    """FastAPI 依赖：Origin 校验 + （条件性）双提交 token 校验。

    失败抛 403（不是 401 —— 身份可能完全有效，只是请求来源/令牌不对）。
    """
    if not csrf_enabled():
        return

    ok, reason = check_request_origin(request)
    if not ok:
        logger.warning(
            "[CSRF] origin rejected: path=%s method=%s reason=%s origin=%r referer=%r",
            request.url.path, request.method, reason,
            (request.headers.get("origin") or "")[:200],
            (request.headers.get("referer") or "")[:200],
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_ORIGIN_FAIL_DETAIL)

    if requires_csrf_token(request) and not csrf_token_matches(request):
        logger.warning(
            "[CSRF] token missing/mismatch: path=%s method=%s",
            request.url.path, request.method,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_CSRF_FAIL_DETAIL)


# ────────────────────────────────────────────────────────────────────
# WebSocket 握手来源校验（浏览器 WS 一定带 Origin，且不带 Origin 的非浏览器
# 客户端不能靠 cookie 被 CSRF 利用 —— 但仍然做同源/白名单检查）
# ────────────────────────────────────────────────────────────────────

def check_ws_origin(origin: Optional[str], host_header: Optional[str],
                    forwarded_proto: Optional[str] = None,
                    forwarded_host: Optional[str] = None) -> Tuple[bool, str]:
    """WS 握手 Origin 校验（不依赖 Request 对象）。

    返回 `(ok, reason)`：
    - `ok=True, reason="origin"` / `"no_origin"`
    - `ok=False, reason="bad_origin"` / `"origin_required"`
    """
    if not csrf_enabled():
        return True, "disabled"

    if origin is None:
        if getattr(settings, "WS_REQUIRE_ORIGIN", False):
            return False, "origin_required"
        return True, "no_origin"

    allowed: List[str] = []
    host = (host_header or "").strip()
    proto = (forwarded_proto or "").split(",")[0].strip().lower() or "http"
    if host:
        # 与 `_request_self_origins` 同口径：TLS 可能在反代终止，两种 scheme 都接受
        for scheme in ("http", "https"):
            norm = normalize_origin(f"{scheme}://{host}")
            if norm and norm not in allowed:
                allowed.append(norm)

    # X-Forwarded-Host 只在**落在 FECLAW_PUBLIC_URL 域下**时才当作同源
    # （与 `_request_self_origins` 同一规则；否则代理/头注入可把外域变成「允许来源」）
    fwd_host = (forwarded_host or "").split(",")[0].strip()
    if fwd_host:
        bare = fwd_host.split(":")[0].lower()
        public_raw = (getattr(settings, "FECLAW_PUBLIC_URL", "") or "").split("://")[-1]
        public_host = public_raw.split("/")[0].split(":")[0].lower() if public_raw else ""
        trusted = (
            (public_host and (bare == public_host or bare.endswith("." + public_host)))
            or (not public_host and bare in ("localhost", "127.0.0.1", "[::1]", "::1"))
        )
        if trusted:
            for scheme in (proto, "https", "http"):
                norm = normalize_origin(f"{scheme}://{fwd_host}")
                if norm and norm not in allowed:
                    allowed.append(norm)
    for item in _public_url_origins() + _configured_extra_origins("WS_EXTRA_ORIGINS") + _configured_extra_origins("CSRF_EXTRA_ORIGINS"):
        if item not in allowed:
            allowed.append(item)
    if not getattr(settings, "FECLAW_PUBLIC_URL", ""):
        for h in ("localhost", "127.0.0.1", "[::1]", "::1"):
            for scheme in ("http", "https"):
                norm = normalize_origin(f"{scheme}://{h}")
                if norm and norm not in allowed:
                    allowed.append(norm)

    if origin_allowed(origin, allowed):
        return True, "origin"
    return False, "bad_origin"


__all__ = [
    "SAFE_METHODS",
    "csrf_cookie_name",
    "csrf_header_name",
    "csrf_enabled",
    "normalize_origin",
    "allowed_origins",
    "origin_allowed",
    "check_request_origin",
    "generate_csrf_token",
    "csrf_token_matches",
    "set_csrf_cookie",
    "issue_csrf_token",
    "clear_csrf_cookie",
    "is_bearer_authenticated",
    "has_session_cookie",
    "requires_csrf_token",
    "csrf_protect",
    "csrf_protect_origin_only",
    "check_ws_origin",
]
