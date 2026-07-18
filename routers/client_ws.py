"""
Client WS 通道
所有客户端（Desktop / Mobile / Web）的统一 WebSocket 入口。

URL 形如: ws://host:port/ws/client?token=<JWT>&channel=<desktop|mobile>&agent_hash=<hash>

参数：
    * token       — JWT，必填
    * channel     — 客户端类型，默认 "desktop"；目前支持 "desktop" | "mobile"
    * agent_hash  — 可选；缺省时为全局连接（旧 /ws/desktop 行为兼容）

Close code 语义（与 FeClaw-Desktop 端约定保持兼容）：
   * 4001 — invalid / expired JWT
   * 4003 — authenticated but does not own the agent
   * 4004 — agent not found

向 client 推送的消息类型：
   * chat_reply / chat_event  — Desktop 渠道（兼容旧 FeClaw-Desktop 协议）
   * direct_message_reply     — Web 私聊用户
   * draft / confirm          — Gen 2 IM Agent 字流（仅 web 渠道）
   * group_message / moments_event / upload_complete / command_exec_request / file_*_request
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from typing import Optional
import asyncio
import json
import logging

from utils.auth import decode_jwt_token
from models.database import SessionLocal

router = APIRouter(prefix="", tags=["client"])
logger = logging.getLogger("client_ws")


class ClientConnectionManager:
    """全局客户端 WS 连接管理器（单连接 — 多客户端由 channel 区分推送目标）。

    进程内只持有一个活跃连接。`channel` 字段标识当前连接是 desktop 还是 mobile，
    服务端推送时按 channel 过滤，避免 desktop-only 事件（如 chat_reply）
    被推到 mobile 客户端。
    """

    def __init__(self):
        self.conn: Optional[WebSocket] = None
        self.channel: str = ""        # "desktop" | "mobile" | ""
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, channel: str = "desktop"):
        await ws.accept()
        async with self.lock:
            self.conn = ws
            self.channel = channel
        logger.info(f"Client WS connected (channel={channel})")

    async def disconnect(self, ws: WebSocket):
        async with self.lock:
            if self.conn == ws:
                self.conn = None
                self.channel = ""
        logger.info("Client WS disconnected")

    async def send(self, message: dict) -> bool:
        """向客户端发送消息。返回是否发送成功。

        静默降级：连接为空或发送失败时返回 False，不抛异常。
        """
        async with self.lock:
            if self.conn is None:
                return False
            try:
                await self.conn.send_json(message)
                return True
            except Exception as e:
                logger.error(f"Failed to send to client: {e}")
                return False

    @property
    def is_connected(self) -> bool:
        return self.conn is not None

    def matches_channel(self, *allowed: str) -> bool:
        """检查当前连接的 channel 是否在允许集合内。

        空 channel（未连接或 legacy 调用）视为不匹配，避免误推。
        """
        if not self.channel:
            return False
        return self.channel in allowed


manager = ClientConnectionManager()


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


@router.websocket("/ws/client")
async def client_websocket(
    ws: WebSocket,
    token: Optional[str] = Query(None),
    channel: str = Query("desktop"),
    agent_hash: Optional[str] = Query(None),
):
    """
    Client WS 统一连接端点。

    URL: ws://host:port/ws/client?token=<JWT>&channel=<desktop|mobile>&agent_hash=<hash>

    鉴权在 accept 之前完成，失败按 4xxx close code 直接断开：
      * 4001 — 缺/无效 token
      * 4003 — token 有效但无权访问该 agent（仅 agent_hash 非空时校验）
      * 4004 — agent 不存在（仅 agent_hash 非空时校验）
    """
    # 1. JWT 校验
    if not token:
        await ws.close(code=4001, reason=b"missing token")
        logger.warning(f"Client WS rejected: missing token (channel={channel})")
        return
    payload = decode_jwt_token(token)
    if not payload or not payload.get("user_id"):
        await ws.close(code=4001, reason=b"invalid token")
        logger.warning(f"Client WS rejected: invalid token (channel={channel})")
        return
    user_id: int = int(payload["user_id"])

    # 2. agent 归属校验（仅当提供 agent_hash 时）
    if agent_hash:
        owns, exists = _user_owns_agent(user_id, agent_hash)
        if not owns:
            if not exists:
                await ws.close(code=4004, reason=b"agent not found")
                logger.warning(f"Client WS rejected: agent not found (hash={agent_hash})")
            else:
                await ws.close(code=4003, reason=b"forbidden")
                logger.warning(
                    f"Client WS rejected: forbidden (user_id={user_id}, hash={agent_hash})"
                )
            return

    # 鉴权通过，正式 accept
    await manager.connect(ws, channel=channel)
    try:
        while True:
            data = await ws.receive_json()
            # 防御性编程：校验消息中的 agent_hash 所有权
            if isinstance(data, dict):
                data.setdefault("user_id", user_id)
                # agent_hash 优先用 URL 上的，回退到消息体
                msg_agent = (
                    agent_hash
                    or data.get("agent_hash")
                    or data.get("agent")
                )
                if msg_agent:
                    owns, exists = _user_owns_agent(user_id, msg_agent)
                    if not owns:
                        logger.warning(
                            f"Client WS rejected agent access: "
                            f"user_id={user_id} agent_hash={msg_agent} (exists={exists})"
                        )
                        # 跳过该消息处理，但不断开连接（用户可能拥有其他 agent）
                        continue
                if agent_hash:
                    data.setdefault("agent_hash", agent_hash)
            await handle_client_message(data)
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception as e:
        logger.error(f"Client WS error: {e}")
        await manager.disconnect(ws)


async def handle_client_message(msg: dict):
    """处理来自 Client 的消息"""
    msg_type = msg.get("type")
    if msg_type == "consent_response":
        # Desktop 同意了某个命令执行，通知等待中的请求
        request_id = msg.get("request_id")
        decision = msg.get("decision")  # "allow" | "deny"
        if request_id and decision:
            from services.desktop_relay import relay
            await relay.resolve_consent(request_id, decision)
    elif msg_type == "pong":
        # 客户端心跳响应，仅记录
        logger.debug(f"Received pong from {manager.channel or 'client'}")
    elif msg_type in ("file_read_response", "file_write_response", "file_delete_response"):
        # 文件操作响应：把 Client 返回的 payload 完整透传给等待者
        request_id = msg.get("id")
        if request_id:
            from services.desktop_relay import relay
            payload = msg.get("payload", {})
            await relay.resolve_response(request_id, payload)
    elif msg_type == "chat_message":
        # Client 聊天消息 → 通过 WebChannelService 处理并回复
        text = msg.get("text", "")
        agent_hash = msg.get("agent_hash") or msg.get("agent", "")
        msg_id = msg.get("id", "")
        user_id = msg.get("user_id")
        if not text or not agent_hash or not user_id:
            logger.warning(f"Client WS: incomplete chat_message (agent={agent_hash}, text_len={len(text)})")
            return
        # 异步处理：不要让 WS 消息循环等待 LLM 响应
        asyncio.ensure_future(_handle_chat_message(user_id, agent_hash, text, msg_id))
    else:
        logger.warning(f"Unknown client message type: {msg_type}")


async def _handle_chat_message(user_id: int, agent_hash: str, text: str, msg_id: str):
    """后台处理 Client 聊天消息并回复"""
    try:
        from services.web_channel_service import WebChannelService
        from models.database import SessionLocal, AgentProfile

        db = SessionLocal()
        try:
            # ── IM Agent 路由：投递 IRQ 后立刻返回 ──
            agent = db.query(AgentProfile).filter(
                AgentProfile.hash == agent_hash
            ).first()
            if agent and getattr(agent, "agent_mode", "classic") == "im":
                try:
                    from services.interrupt_controller import (
                        InterruptController,
                        Interrupt,
                        InterruptType,
                        Priority,
                    )
                    ic = InterruptController.instance()
                    ic.dispatch(Interrupt(
                        irq_type=InterruptType.MESSAGE,
                        agent_hash=agent_hash,
                        priority=Priority.HIGH,
                        payload={
                            "channel": "desktop",
                            "user_id": user_id,
                            "agent_hash": agent_hash,
                            "msg_id": msg_id,
                            "trigger_content": text[:1000],
                            "trigger_sender": "用户",
                        },
                    ))
                    logger.info(
                        f"[Client] IM Agent IRQ 投递 agent={agent_hash} "
                        f"user={user_id} msg_id={msg_id}"
                    )
                except Exception as e:
                    logger.warning(f"[Client] IM Agent IRQ 投递失败: {e}")
                return

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
                        await send_to_client(chat_event)
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
                    await send_to_client(chat_reply)
                    # 发送完成事件
                    done_event = {
                        "type": "chat_event",
                        "id": msg_id,
                        "kind": "done",
                        "data": {"session_id": data_str} if data_str else {},
                        "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    }
                    await send_to_client(done_event)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Client chat error: {e}", exc_info=True)
        error_reply = {
            "type": "chat_reply",
            "id": msg_id,
            "text": f"抱歉，处理消息时出错了：{str(e)}",
            "agent": agent_hash,
            "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        }
        await send_to_client(error_reply)


# 提供给其他模块调用的发送接口
async def send_to_client(message: dict) -> bool:
    """统一客户端推送入口（替代旧 send_to_desktop）。"""
    return await manager.send(message)


# 旧 API 兼容别名（过渡期保留）
async def send_to_desktop(message: dict) -> bool:
    """兼容旧调用 — 等价于 send_to_client()。

    与 desktop_ws 时代的语义保持一致：把消息推给 manager.conn
    （若当前 channel 是 mobile，仍会发送 — 调用方有责任按 channel 过滤）。
    推荐使用 send_to_client() 或 manager.send() 配合 matches_channel()。
    """
    return await manager.send(message)