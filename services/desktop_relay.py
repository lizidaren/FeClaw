"""
Desktop 执行请求中继服务
Desktop 模式下，Agent 执行命令时通过此服务向 Desktop WS 客户端发送授权请求，
以及 file_read / file_write / file_delete 文件桥接请求。

路径映射（与 FeClaw-Desktop 端 Rust 实现保持一致）：
   /mnt/desktop/C:/...   → Windows 绝对路径
   /mnt/desktop/foo.txt   → ~/Desktop/foo.txt
   /mnt/desktop/sub/...   → ~/Desktop/sub/...
"""

import asyncio
import os
import platform
import uuid
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("desktop_relay")


# ─────────────────────────────────────────────────────────────────────
# /mnt/desktop/ 路径映射
# ─────────────────────────────────────────────────────────────────────


class DesktopPathError(ValueError):
    """路径穿越或不合法"""


def resolve_desktop_path(vfs_path: str) -> str:
    """
    将 /mnt/desktop/... 解析为本地文件系统绝对路径。

    规则：
      * /mnt/desktop/C:/...  → C:\\...   (Windows 主机)
      * /mnt/desktop/foo.txt → ~/Desktop/foo.txt

    Raises:
        DesktopPathError: 路径不以 /mnt/desktop/ 开头、为空、含 `..` 段、
                          或在非 Windows 主机上收到 Windows 绝对路径。
    """
    if not vfs_path.startswith("/mnt/desktop/"):
        raise DesktopPathError(f"not under /mnt/desktop/: {vfs_path}")
    stripped = vfs_path[len("/mnt/desktop/"):]
    if not stripped:
        raise DesktopPathError("empty /mnt/desktop/ path")

    # 阻断 .. 段（路径穿越防护）
    for seg in stripped.replace("\\", "/").split("/"):
        if seg == "..":
            raise DesktopPathError(f"path traversal blocked: {vfs_path}")

    # Windows 绝对路径（C:/..., D:\...）直通
    if len(stripped) >= 2 and stripped[0].isalpha() and stripped[1] in (":", "\\", "/"):
        if platform.system() != "Windows":
            raise DesktopPathError(
                f"windows absolute path on non-Windows host: {vfs_path}"
            )
        drive = stripped[0:2]
        rest = stripped[2:].replace("\\", "/").lstrip("/")
        if not rest:
            return drive + os.sep
        return os.path.join(drive + os.sep, *rest.split("/"))

    # 默认：~/Desktop
    home = Path.home()
    desktop = home / "Desktop"
    return str(desktop / stripped)


def path_exists(vfs_path: str) -> bool:
    """单次解析：resolve + os.path.exists。"""
    resolved = resolve_desktop_path(vfs_path)
    return os.path.exists(resolved)


async def file_read(vfs_path: str) -> str:
    """读取 /mnt/desktop/... 指向的文件内容（UTF-8）。"""
    resolved = resolve_desktop_path(vfs_path)
    return await asyncio.to_thread(_sync_read, resolved)


def _sync_read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


async def file_write(vfs_path: str, content: str) -> None:
    """写入 /mnt/desktop/... 指向的文件，必要时自动创建父目录。"""
    resolved = resolve_desktop_path(vfs_path)
    await asyncio.to_thread(_sync_write, resolved, content)


def _sync_write(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


async def file_delete(vfs_path: str) -> None:
    """删除 /mnt/desktop/... 指向的文件。"""
    resolved = resolve_desktop_path(vfs_path)
    await asyncio.to_thread(_sync_delete, resolved)


def _sync_delete(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    os.remove(path)


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
        from routers.client_ws import send_to_client
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

        sent = await send_to_client(msg)
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

    # ─────────────────────────────────────────────────────────────
    # 文件桥接 (file_read / file_write / file_delete)
    # ─────────────────────────────────────────────────────────────
    async def request_file_read(self, path: str) -> dict:
        """请求 Desktop 读取文件；Desktop 通过 file_read_response 返回。"""
        request_id = str(uuid.uuid4())
        future = asyncio.get_event_loop().create_future()
        self.pending[request_id] = future
        from routers.client_ws import send_to_client
        msg = {"type": "file_read_request", "id": request_id, "payload": {"path": path}}
        sent = await send_to_client(msg)
        if not sent:
            self.pending.pop(request_id, None)
            return {"status": "error", "error": "Desktop not connected"}
        try:
            return await asyncio.wait_for(future, timeout=60)
        except asyncio.TimeoutError:
            return {"status": "error", "error": "timeout"}
        finally:
            self.pending.pop(request_id, None)

    async def request_file_write(self, path: str, content: str) -> dict:
        """请求 Desktop 写入文件；Desktop 通过 file_write_response 返回。"""
        request_id = str(uuid.uuid4())
        future = asyncio.get_event_loop().create_future()
        self.pending[request_id] = future
        from routers.client_ws import send_to_client
        msg = {
            "type": "file_write_request",
            "id": request_id,
            "payload": {"path": path, "content": content},
        }
        sent = await send_to_client(msg)
        if not sent:
            self.pending.pop(request_id, None)
            return {"status": "error", "error": "Desktop not connected"}
        try:
            return await asyncio.wait_for(future, timeout=60)
        except asyncio.TimeoutError:
            return {"status": "error", "error": "timeout"}
        finally:
            self.pending.pop(request_id, None)

    async def request_file_delete(self, path: str) -> dict:
        """请求 Desktop 删除文件；Desktop 通过 file_delete_response 返回。"""
        request_id = str(uuid.uuid4())
        future = asyncio.get_event_loop().create_future()
        self.pending[request_id] = future
        from routers.client_ws import send_to_client
        msg = {
            "type": "file_delete_request",
            "id": request_id,
            "payload": {"path": path},
        }
        sent = await send_to_client(msg)
        if not sent:
            self.pending.pop(request_id, None)
            return {"status": "error", "error": "Desktop not connected"}
        try:
            return await asyncio.wait_for(future, timeout=60)
        except asyncio.TimeoutError:
            return {"status": "error", "error": "timeout"}
        finally:
            self.pending.pop(request_id, None)

    async def resolve_consent(self, request_id: str, decision: str):
        """收到 Desktop 的 consent_response 后调用此方法唤醒等待中的请求"""
        future = self.pending.get(request_id)
        if future and not future.done():
            future.set_result({"decision": decision})

    async def resolve_response(self, request_id: str, payload: dict):
        """
        收到 Desktop 的 file_*_response 后调用此方法唤醒等待中的请求。
        把 Desktop 返回的完整 payload 透传给等待者。
        """
        future = self.pending.pop(request_id, None)
        if future and not future.done():
            future.set_result(payload)

    def is_desktop_connected(self) -> bool:
        from routers.client_ws import manager
        return manager.is_connected


relay = DesktopRelay()
