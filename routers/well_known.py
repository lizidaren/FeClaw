from __future__ import annotations

"""
FeClaw Desktop 客户端发现端点 (RFC 8615 .well-known).

Desktop 客户端启动时通过 `GET /.well-known/feclaw-desktop` 获取：
  * 服务版本号（用于客户端兼容性检查）
  * 服务名称（用于展示）
  * 认证方式：
       - `local`  → 本地模式（用户名/密码）
       - `platform` → OAuth / Platform 模式（`/api/auth/login`）
       - 未来可能扩展 `oidc` 等
  * 认证端点（`platform` 模式下为 `POST {endpoint}/api/auth/login`）
  * WebSocket 路径（`/ws/client?token=<JWT>&channel=desktop&agent_hash=<hash>`）

该端点只返回元信息，不泄露任何敏感数据。
"""

from fastapi import APIRouter

from config import settings as app_settings

router = APIRouter(tags=["well-known"])


@router.get("/.well-known/feclaw-desktop")
async def feclaw_desktop_discovery() -> dict:
    """
    FeClaw Desktop 客户端的发现端点。

    返回的 `auth.endpoint` 是登录接口的 base URL，客户端会向其
    追加 `/api/auth/login` 形成完整请求地址。

    当 OAuth 未启用或未配置 OAUTH_PROVIDER_URL 时，`auth.endpoint`
    为 `None`，表示这是一个"本地模式"部署（用户名/密码直接对 engine
    自身的 `/api/user/login` 登录）。
    """
    auth_type = "platform" if app_settings.OAUTH_ENABLED else "local"

    auth_endpoint: str | None = None
    if app_settings.OAUTH_ENABLED and app_settings.OAUTH_PROVIDER_URL:
        # 去掉末尾的斜杠，避免与 `/api/auth/login` 拼接时出现 `//`
        auth_endpoint = f"{app_settings.OAUTH_PROVIDER_URL.rstrip('/')}/api/auth/login"

    return {
        "version": "1",
        "name": "FeClaw",
        "auth": {
            "type": auth_type,
            "endpoint": auth_endpoint,
        },
        "ws_path": "/ws/client",
    }
