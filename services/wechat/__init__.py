"""
WeChat service package - iLink协议实现

Submodules:
- models: 常量定义（消息类型、错误码等）
- sdk_adapter: DatabaseBackedClient + HTTP helpers + CDN 下载
"""

from .models import (
    MSG_TYPE_TEXT, MSG_TYPE_IMAGE, MSG_TYPE_VOICE, MSG_TYPE_VIDEO, MSG_TYPE_BOT,
    ERR_SESSION_EXPIRED,
    WATCHDOG_INTERVAL, INACTIVITY_THRESHOLD, MAX_RESTART_COUNT,
    ILINK_API_BASE, ILINK_CDN_BASE,
    get_msg_type_name,
)

from .sdk_adapter import (
    DatabaseBackedClient,
    download_wechat_image_from_media,
    random_wechat_uin,
    auth_headers,
)

__all__ = [
    "MSG_TYPE_TEXT", "MSG_TYPE_IMAGE", "MSG_TYPE_VOICE", "MSG_TYPE_VIDEO", "MSG_TYPE_BOT",
    "ERR_SESSION_EXPIRED",
    "WATCHDOG_INTERVAL", "INACTIVITY_THRESHOLD", "MAX_RESTART_COUNT",
    "ILINK_API_BASE", "ILINK_CDN_BASE",
    "get_msg_type_name",
    "DatabaseBackedClient",
    "download_wechat_image_from_media",
    "random_wechat_uin",
    "auth_headers",
]
