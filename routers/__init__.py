# FeClaw Backend Routers

from routers.console import router as console_router
from routers.feclaw_chat import router as feclaw_chat_router
from routers.health import router as health_router
from routers.oauth import router as oauth_router
from routers.sandbox import router as sandbox_router
from routers.share import router as share_router
from routers.share_reference import router as share_reference_router
from routers.static_site import router as static_site_router
from routers.user import router as user_router
from routers.wechat import router as wechat_router
from routers.workspace import router as workspace_router


__all__ = [
    "console_router",
    "feclaw_chat_router",
    "health_router",
    "oauth_router",
    "sandbox_router",
    "share_router",
    "share_reference_router",
    "static_site_router",
    "user_router",
    "wechat_router",
    "workspace_router",
]