"""
Desktop WS 通道
Desktop 连接进来的入口，提供命令执行授权弹窗的 WebSocket 中转
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Dict, Optional
import asyncio
import json
import logging

router = APIRouter(prefix="", tags=["desktop"])
logger = logging.getLogger("desktop_ws")

# 全局 Desktop 连接管理器
class DesktopConnectionManager:
    def __init__(self):
        self.conn: Optional[WebSocket] = None
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.conn = ws
        logger.info("Desktop WS connected")

    async def disconnect(self, ws: WebSocket):
        async with self.lock:
            if self.conn == ws:
                self.conn = None
        logger.info("Desktop WS disconnected")

    async def send(self, message: dict) -> bool:
        """向 Desktop 发送消息。返回是否发送成功。"""
        async with self.lock:
            if self.conn is None:
                return False
            try:
                await self.conn.send_json(message)
                return True
            except Exception as e:
                logger.error(f"Failed to send to Desktop: {e}")
                return False

    @property
    def is_connected(self) -> bool:
        return self.conn is not None


manager = DesktopConnectionManager()


@router.websocket("/ws/desktop")
async def desktop_websocket(ws: WebSocket):
    """Desktop WS 连接端点。Desktop 主动连接此端点建立长连接。"""
    await manager.connect(ws)
    try:
        while True:
            # 接收来自 Desktop 的消息（consent_response 等）
            data = await ws.receive_json()
            await handle_desktop_message(data)
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception as e:
        logger.error(f"Desktop WS error: {e}")
        await manager.disconnect(ws)


async def handle_desktop_message(msg: dict):
    """处理来自 Desktop 的消息"""
    msg_type = msg.get("type")
    if msg_type == "consent_response":
        # Desktop 同意了某个命令执行，通知等待中的请求
        request_id = msg.get("request_id")
        decision = msg.get("decision")  # "allow" | "deny"
        if request_id and decision:
            from services.desktop_relay import relay
            await relay.resolve_consent(request_id, decision)
    elif msg_type == "pong":
        # Desktop 心跳响应，仅记录
        logger.debug("Received pong from Desktop")
    else:
        logger.warning(f"Unknown desktop message type: {msg_type}")


# 提供给其他模块调用的发送接口
async def send_to_desktop(message: dict) -> bool:
    return await manager.send(message)
