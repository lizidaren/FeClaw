"""
WeChat 常量定义和数据结构

包括消息类型、错误码、看门狗配置、连接配置等。
"""
from typing import Dict, Optional
from dataclasses import dataclass
from datetime import datetime


# ========== 消息类型 ==========
MSG_TYPE_TEXT = 1
MSG_TYPE_IMAGE = 3
MSG_TYPE_VOICE = 4
MSG_TYPE_VIDEO = 43
MSG_TYPE_BOT = 2  # BOT 消息

# ========== 消息类型名称映射 ==========
MSG_TYPE_NAMES = {
    1: "text",
    3: "image",
    4: "voice",
    43: "video",
    2: "bot",
}

# ========== 错误码 ==========
ERR_SESSION_EXPIRED = -14  # 会话过期，需要重新登录

# ========== 看门狗配置 ==========
WATCHDOG_INTERVAL = 30      # 检查间隔（秒）
INACTIVITY_THRESHOLD = 60   # 不活跃阈值（秒）
MAX_RESTART_COUNT = 3       # 每个 user_id 最大连续重启次数

# ========== iLink API 配置 ==========
ILINK_API_BASE = "https://ilinkai.weixin.qq.com"
ILINK_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"


def get_msg_type_name(msg_type: int) -> str:
    """消息类型编号转名称"""
    return MSG_TYPE_NAMES.get(msg_type, f"unknown_{msg_type}")
