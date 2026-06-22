"""
Desktop WS 通道
Desktop 连接进来的入口，提供命令执行授权弹窗的 WebSocket 中转

URL 形如: ws://host:port/ws/desktop/{agent_hash}?token=<JWT>

Close code 语义（与 FeClaw-Desktop 端约定）：
   * 4001 — invalid / expired JWT
   * 4003 — authenticated but does not own the agent
   * 4004 — agent not found
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from typing import Optional
import asyncio
import json
import logging

from utils.auth import decode_jwt_token
from models.database import SessionLocal

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


def _user_owns_agent(user_id: int, agent_hash: str) -> tuple[bool, bool]:
    """
    校验 user_id 是否拥有 agent_hash。

    Returns:
        (owns, exists) — owns=True 时 user 与 agent 匹配；
                          exists=True 时 agent 存在于 DB（owns=False 但 exists=True 表示无权访问）
    """
    from models.agent_profile import AgentProfile

    db = SessionLocal()
    try:
        agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
        if agent is None:
            return False, False
        return agent.user_id == user_id, True
    finally:
        db.close()


@router.websocket("/ws/desktop")
async def desktop_websocket_global(
    ws: WebSocket,
    token: Optional[str] = Query(None),
):
    """
    Desktop WS 全局连接端点（无 agent_hash）。

    与 /ws/desktop/{agent_hash} 不同，此端点不要求 agent_hash，
    适用于 Desktop 客户端建立单一 WS 连接的情况。

    Close codes:
      * 4001 — invalid / expired JWT
    """
    # 1. JWT 校验
    if not token:
        await ws.close(code=4001, reason=b"missing token")
        logger.warning("Desktop WS (global) rejected: missing token")
        return
    payload = decode_jwt_token(token)
    if not payload or not payload.get("user_id"):
        await ws.close(code=4001, reason=b"invalid token")
        logger.warning("Desktop WS (global) rejected: invalid token")
        return
    user_id: int = int(payload["user_id"])

    # 鉴权通过，正式 accept
    await manager.connect(ws)
    try:
        while True:
            data = await ws.receive_json()
            # 防御性编程：校验消息中的 agent_hash 所有权
            if isinstance(data, dict):
                data.setdefault("user_id", user_id)
                msg_agent = data.get("agent_hash") or data.get("agent")
                if msg_agent:
                    owns, exists = _user_owns_agent(user_id, msg_agent)
                    if not owns:
                        logger.warning(
                            f"Desktop WS (global) rejected agent access: "
                            f"user_id={user_id} agent_hash={msg_agent} (exists={exists})"
                        )
                        # 跳过该消息处理，但不断开连接（用户可能拥有其他 agent）
                        continue
            await handle_desktop_message(data)
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception as e:
        logger.error(f"Desktop WS (global) error: {e}")
        await manager.disconnect(ws)


@router.websocket("/ws/desktop/{agent_hash}")
async def desktop_websocket(
    ws: WebSocket,
    agent_hash: str,
    token: Optional[str] = Query(None),
):
    """
    Desktop WS 连接端点。

    鉴权在 accept 之前完成，失败按 4xxx close code 直接断开：
      * 4001 — 缺/无效 token
      * 4003 — token 有效但无权访问该 agent
      * 4004 — agent 不存在
    """
    # 1. JWT 校验
    if not token:
        await ws.close(code=4001, reason=b"missing token")
        logger.warning("Desktop WS rejected: missing token")
        return
    payload = decode_jwt_token(token)
    if not payload or not payload.get("user_id"):
        await ws.close(code=4001, reason=b"invalid token")
        logger.warning("Desktop WS rejected: invalid token")
        return
    user_id: int = int(payload["user_id"])

    # 2. agent 归属校验
    owns, exists = _user_owns_agent(user_id, agent_hash)
    if not owns:
        if not exists:
            await ws.close(code=4004, reason=b"agent not found")
            logger.warning(f"Desktop WS rejected: agent not found (hash={agent_hash})")
        else:
            await ws.close(code=4003, reason=b"forbidden")
            logger.warning(
                f"Desktop WS rejected: forbidden (user_id={user_id}, hash={agent_hash})"
            )
        return

    # 鉴权通过，正式 accept
    await manager.connect(ws)
    try:
        while True:
            data = await ws.receive_json()
            # 把 agent_hash 注入到消息上下文，供 relay 使用
            if isinstance(data, dict):
                data.setdefault("agent_hash", agent_hash)
                data.setdefault("user_id", user_id)
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
    elif msg_type in ("file_read_response", "file_write_response", "file_delete_response"):
        # 文件操作响应：把 Desktop 返回的 payload 完整透传给等待者
        request_id = msg.get("id")
        if request_id:
            from services.desktop_relay import relay
            payload = msg.get("payload", {})
            await relay.resolve_response(request_id, payload)
    elif msg_type == "chat_message":
        # Desktop 聊天消息 → 通过 WebChannelService 处理并回复
        text = msg.get("text", "")
        agent_hash = msg.get("agent_hash") or msg.get("agent", "")
        msg_id = msg.get("id", "")
        user_id = msg.get("user_id")
        if not text or not agent_hash or not user_id:
            logger.warning(f"Desktop WS: incomplete chat_message (agent={agent_hash}, text_len={len(text)})")
            return
        # 异步处理：不要让 WS 消息循环等待 LLM 响应
        asyncio.ensure_future(_handle_chat_message(user_id, agent_hash, text, msg_id))
    else:
        logger.warning(f"Unknown desktop message type: {msg_type}")


async def _handle_chat_message(user_id: int, agent_hash: str, text: str, msg_id: str):
    """后台处理 Desktop 聊天消息并回复"""
    try:
        from services.web_channel_service import WebChannelService
        from models.database import SessionLocal

        db = SessionLocal()
        try:
            chat_service = WebChannelService(db, user_id=user_id, agent_hash=agent_hash)
            full_response = ""
            async for sse_str in chat_service.chat_stream(text):
                # SSE 格式: "event: token\ndata: {...}\n\n"
                if not sse_str.startswith("event: "):
                    continue
                lines = sse_str.strip().split("\n")
                event_type = None
                data_str = None
                for line in lines:
                    if line.startswith("event: "):
                        event_type = line[7:]
                    elif line.startswith("data: "):
                        data_str = line[6:]
                if event_type == "token" and data_str:
                    try:
                        payload = json.loads(data_str)
                        token = payload.get("content", "")
                        full_response += token
                        chat_event = {
                            "type": "chat_event",
                            "id": msg_id,
                            "kind": "token",
                            "data": {"delta": token},
                            "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                        }
                        await send_to_desktop(chat_event)
                    except json.JSONDecodeError:
                        pass
                elif event_type == "done":
                    # 发送最终响应
                    chat_reply = {
                        "type": "chat_reply",
                        "id": msg_id,
                        "text": full_response,
                        "agent": agent_hash,
                        "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    }
                    await send_to_desktop(chat_reply)
                    # 发送完成事件
                    done_event = {
                        "type": "chat_event",
                        "id": msg_id,
                        "kind": "done",
                        "data": {"session_id": data_str} if data_str else {},
                        "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    }
                    await send_to_desktop(done_event)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Desktop chat error: {e}", exc_info=True)
        error_reply = {
            "type": "chat_reply",
            "id": msg_id,
            "text": f"抱歉，处理消息时出错了：{str(e)}",
            "agent": agent_hash,
            "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        }
        await send_to_desktop(error_reply)


# 提供给其他模块调用的发送接口
async def send_to_desktop(message: dict) -> bool:
    return await manager.send(message)
