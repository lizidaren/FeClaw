"""
Desktop 执行请求中继服务
Desktop 模式下，Agent 执行命令时通过此服务向 Desktop WS 客户端发送授权请求
"""

import asyncio
import uuid
import logging
from typing import Optional

logger = logging.getLogger("desktop_relay")


class DesktopRelay:
    def __init__(self):
        self.pending: dict[str, asyncio.Future] = {}

    async def request_consent(
        self,
        command: str,
        args: list[str],
        cwd: str,
        risk_level: int,
    ) -> dict:
        """
        向 Desktop 请求命令执行授权。
        发送 command_exec_request 消息给 Desktop WS 客户端，
        等待 consent_response 后返回。

        Returns:
            {"decision": "allow" | "deny", "reason": str}
        """
        request_id = str(uuid.uuid4())
        future = asyncio.get_event_loop().create_future()
        self.pending[request_id] = future

        # 构造发送给 Desktop 的消息
        from routers.desktop_ws import send_to_desktop
        msg = {
            "type": "command_exec_request",
            "id": request_id,
            "payload": {
                "command": command,
                "args": args,
                "cwd": cwd,
                "risk_level": risk_level,
            }
        }

        sent = await send_to_desktop(msg)
        if not sent:
            self.pending.pop(request_id, None)
            return {"decision": "deny", "reason": "Desktop not connected"}

        # 等待 Desktop 返回（超时 5 分钟）
        try:
            result = await asyncio.wait_for(future, timeout=300)
            return result
        except asyncio.TimeoutError:
            self.pending.pop(request_id, None)
            return {"decision": "deny", "reason": "Desktop timeout"}
        finally:
            self.pending.pop(request_id, None)

    async def resolve_consent(self, request_id: str, decision: str):
        """收到 Desktop 的 consent_response 后调用此方法唤醒等待中的请求"""
        future = self.pending.get(request_id)
        if future and not future.done():
            future.set_result({"decision": decision})

    def is_desktop_connected(self) -> bool:
        from routers.desktop_ws import manager
        return manager.is_connected


relay = DesktopRelay()
