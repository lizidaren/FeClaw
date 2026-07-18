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
from typing import Dict, Optional, Set, Tuple
import asyncio
import json
import logging

from utils.auth import decode_jwt_token
from models.database import SessionLocal

router = APIRouter(prefix="", tags=["client"])
logger = logging.getLogger("client_ws")


class WSRoom:
    """按 ``(user_id, session_id)`` 隔离的客户端 WebSocket 房间。"""

    def __init__(self):
        self.connections: Dict[Tuple[int, str], Set[WebSocket]] = {}
        self._metadata: Dict[WebSocket, Dict[str, Optional[str]]] = {}
        self.lock = asyncio.Lock()

    async def connect(
        self,
        ws: WebSocket,
        user_id: int,
        session_id: str,
        channel: str = "desktop",
        agent_hash: Optional[str] = None,
    ) -> None:
        await ws.accept()
        key = (user_id, session_id)
        async with self.lock:
            self.connections.setdefault(key, set()).add(ws)
            self._metadata[ws] = {
                "channel": channel,
                "agent_hash": agent_hash,
                "session_id": session_id,
            }
        logger.info(
            "Client WS connected (user_id=%s, session_id=%s, channel=%s)",
            user_id,
            session_id,
            channel,
        )

    async def disconnect(self, ws: WebSocket) -> None:
        async with self.lock:
            empty_keys = []
            for key, sockets in self.connections.items():
                sockets.discard(ws)
                if not sockets:
                    empty_keys.append(key)
            for key in empty_keys:
                self.connections.pop(key, None)
            self._metadata.pop(ws, None)
        logger.info("Client WS disconnected")

    async def _send_many(self, sockets: Set[WebSocket], message: dict) -> bool:
        sent = False
        stale: Set[WebSocket] = set()
        for ws in sockets:
            try:
                await ws.send_json(message)
                sent = True
            except Exception as exc:
                stale.add(ws)
                logger.warning("Failed to send to client WS: %s", exc)
        for ws in stale:
            await self.disconnect(ws)
        return sent

    async def send_to(
        self,
        user_id: int,
        session_id: str,
        message: dict,
    ) -> bool:
        """只向指定用户的指定会话推送。"""
        key = (int(user_id), str(session_id))
        async with self.lock:
            sockets = set(self.connections.get(key, set()))
        if not sockets:
            return False
        return await self._send_many(sockets, message)

    async def send(self, message: dict) -> bool:
        """兼容旧调用，并在可推断路由时坚持最小范围投递。

        新代码应在 payload 中携带 ``user_id`` 和 ``session_id``，或直接调用
        :meth:`send_to`。无路由信息时仅在全进程恰有一个连接的 legacy 场景发送，
        防止多用户环境下广播敏感消息。
        """
        user_id = message.get("user_id")
        session_id = message.get("session_id")
        if user_id is not None and session_id:
            return await self.send_to(int(user_id), str(session_id), message)

        async with self.lock:
            if user_id is not None:
                sockets = {
                    ws
                    for (key_user_id, _), room in self.connections.items()
                    if key_user_id == int(user_id)
                    for ws in room
                }
            elif session_id:
                sockets = {
                    ws
                    for (_, key_session_id), room in self.connections.items()
                    if key_session_id == str(session_id)
                    for ws in room
                }
            else:
                agent_hash = message.get("agent_hash") or message.get("agent")
                if agent_hash:
                    sockets = {
                        ws
                        for ws, meta in self._metadata.items()
                        if meta.get("agent_hash") == str(agent_hash)
                    }
                else:
                    all_sockets = set(self._metadata)
                    sockets = all_sockets if len(all_sockets) == 1 else set()
        if not sockets:
            logger.warning("Skipped unroutable client WS event type=%s", message.get("type"))
            return False
        return await self._send_many(sockets, message)

    def has_connection(self, user_id: int, session_id: str) -> bool:
        return bool(self.connections.get((int(user_id), str(session_id))))

    @property
    def is_connected(self) -> bool:
        return any(self.connections.values())

    @property
    def channel(self) -> str:
        """Legacy 单连接兼容属性；多连接时不返回含糊的 channel。"""
        if len(self._metadata) != 1:
            return ""
        return next(iter(self._metadata.values())).get("channel") or ""

    def matches_channel(self, *allowed: str) -> bool:
        """是否至少有一个连接属于指定渠道。"""
        return any(meta.get("channel") in allowed for meta in self._metadata.values())


manager = WSRoom()


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


async def _reject_ws(
    ws: WebSocket,
    *,
    close_code: int,
    code: str,
    message: str,
) -> None:
    """发送结构化错误帧后，以约定的 4xxx code 关闭连接。"""
    reason = {"code": code, "message": message}
    await ws.accept()
    await ws.send_json({"type": "error", "reason": reason, **reason})
    await ws.close(
        code=close_code,
        reason=json.dumps(reason, ensure_ascii=False),
    )


@router.websocket("/ws/client")
async def client_websocket(
    ws: WebSocket,
    token: Optional[str] = Query(None),
    channel: str = Query("desktop"),
    agent_hash: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
):
    """
    Client WS 统一连接端点。

    URL: ws://host:port/ws/client?token=<JWT>&channel=<desktop|mobile>&agent_hash=<hash>

    鉴权在 accept 之前完成，失败按 4xxx close code 直接断开：
      * 4001 — 缺/无效 token
      * 4003 — token 有效但无权访问该 agent（仅 agent_hash 非空时校验）
      * 4004 — agent 不存在（仅 agent_hash 非空时校验）
    """
    # 1. JWT 校验。Mobile 依赖 error frame + close code 4001 触发统一登出。
    if not token:
        await _reject_ws(
            ws,
            close_code=4001,
            code="unauthorized",
            message="token_invalid",
        )
        logger.warning(f"Client WS rejected: missing token (channel={channel})")
        return
    payload = decode_jwt_token(token)
    if not payload or not payload.get("user_id"):
        await _reject_ws(
            ws,
            close_code=4001,
            code="unauthorized",
            message="token_invalid",
        )
        logger.warning(f"Client WS rejected: invalid token (channel={channel})")
        return
    user_id: int = int(payload["user_id"])

    # 2. session 绑定校验。Mobile 可以只传 session_id，由后端解析 agent_hash。
    if session_id:
        from models.database import ConversationSession

        db = SessionLocal()
        try:
            bound_session = db.query(ConversationSession).filter(
                ConversationSession.session_id == session_id,
                ConversationSession.user_id == user_id,
            ).first()
        finally:
            db.close()
        if bound_session is None:
            await _reject_ws(
                ws,
                close_code=4004,
                code="session_not_found",
                message="session_not_found",
            )
            logger.warning(
                "Client WS rejected: session not found (user_id=%s, session_id=%s)",
                user_id,
                session_id,
            )
            return
        if agent_hash and agent_hash != bound_session.agent_hash:
            await _reject_ws(
                ws,
                close_code=4003,
                code="forbidden",
                message="agent_session_mismatch",
            )
            return
        agent_hash = bound_session.agent_hash

    # 3. agent 归属校验（session 解析或 query 显式提供时校验）
    if agent_hash:
        owns, exists = _user_owns_agent(user_id, agent_hash)
        if not owns:
            if not exists:
                await _reject_ws(
                    ws,
                    close_code=4004,
                    code="agent_not_found",
                    message="agent_not_found",
                )
                logger.warning(f"Client WS rejected: agent not found (hash={agent_hash})")
            else:
                await _reject_ws(
                    ws,
                    close_code=4003,
                    code="forbidden",
                    message="agent_not_owned",
                )
                logger.warning(
                    f"Client WS rejected: forbidden (user_id={user_id}, hash={agent_hash})"
                )
            return

    # 鉴权通过，正式 accept。Legacy desktop 没有会话时使用用户内稳定占位 room。
    room_session_id = session_id or f"__{channel}__:{agent_hash or 'global'}"
    await manager.connect(
        ws,
        user_id=user_id,
        session_id=room_session_id,
        channel=channel,
        agent_hash=agent_hash,
    )
    try:
        while True:
            data = await ws.receive_json()
            # 防御性编程：校验消息中的 agent_hash 所有权
            if isinstance(data, dict):
                data.setdefault("user_id", user_id)
                data.setdefault("channel", channel)
                if session_id:
                    data.setdefault("session_id", session_id)
                # agent_hash 优先用 URL / session 绑定值，回退到消息体
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
        session_id = msg.get("session_id")
        channel = msg.get("channel") or "desktop"
        if not text or not agent_hash or not user_id:
            logger.warning(f"Client WS: incomplete chat_message (agent={agent_hash}, text_len={len(text)})")
            return
        # 异步处理：不要让 WS 消息循环等待 LLM 响应
        asyncio.ensure_future(
            _handle_chat_message(
                user_id,
                agent_hash,
                text,
                msg_id,
                session_id=session_id,
                channel=channel,
            )
        )
    else:
        logger.warning(f"Unknown client message type: {msg_type}")


async def _handle_chat_message(
    user_id: int,
    agent_hash: str,
    text: str,
    msg_id: str,
    *,
    session_id: Optional[str] = None,
    channel: str = "desktop",
):
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
                            "channel": channel,
                            "user_id": user_id,
                            "agent_hash": agent_hash,
                            "msg_id": msg_id,
                            "session_id": session_id,
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

            chat_service = WebChannelService(
                db,
                user_id=user_id,
                agent_hash=agent_hash,
                channel=channel,
            )
            full_response = ""
            async for sse_str in chat_service.chat_stream(
                text,
                session_id=session_id,
                channel=channel,
            ):
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
                        await send_to_client(
                            chat_event,
                            user_id=user_id,
                            session_id=session_id,
                            agent_hash=agent_hash,
                        )
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
                    await send_to_client(
                        chat_reply,
                        user_id=user_id,
                        session_id=session_id,
                        agent_hash=agent_hash,
                    )
                    # 发送完成事件
                    done_event = {
                        "type": "chat_event",
                        "id": msg_id,
                        "kind": "done",
                        "data": {"session_id": data_str} if data_str else {},
                        "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    }
                    await send_to_client(
                        done_event,
                        user_id=user_id,
                        session_id=session_id,
                        agent_hash=agent_hash,
                    )
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
async def send_to_client(
    message: dict,
    *,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
    agent_hash: Optional[str] = None,
) -> bool:
    """统一客户端推送入口；优先使用显式 user/session 精确路由。"""
    if user_id is not None and session_id:
        return await manager.send_to(user_id, session_id, message)
    routed_message = dict(message)
    if user_id is not None:
        routed_message.setdefault("user_id", user_id)
    if session_id:
        routed_message.setdefault("session_id", session_id)
    if agent_hash:
        routed_message.setdefault("agent_hash", agent_hash)
    return await manager.send(routed_message)


# 旧 API 兼容别名（过渡期保留）
async def send_to_desktop(message: dict) -> bool:
    """兼容旧调用 — 等价于 send_to_client()。

    与 desktop_ws 时代的语义保持一致：把消息推给 manager.conn
    （若当前 channel 是 mobile，仍会发送 — 调用方有责任按 channel 过滤）。
    推荐使用 send_to_client() 或 manager.send() 配合 matches_channel()。
    """
    return await manager.send(message)