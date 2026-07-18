"""
FeClaw 流式聊天路由

POST /api/chat/stream - 流式聊天 (SSE)
WS /api/chat/ws - 流式聊天 (WebSocket)
GET /api/chat/sessions - 获取会话列表
GET /api/chat/sessions/{session_id} - 获取会话详情
DELETE /api/chat/sessions/{session_id} - 删除会话
"""
import json
import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from sqlalchemy.orm import Session
from models.database import SessionLocal, get_db
from config import settings
from routers.feclaw_domain import extract_hash_from_host
from services.web_channel_service import WebChannelService
from utils.auth import get_current_user_id, decode_jwt_token


router = APIRouter(tags=["FeClaw Chat"])


# ========== 请求模型 ==========

class ChatRequest(BaseModel):
    """聊天请求"""
    content: str
    session_id: Optional[str] = None
    image_url: Optional[str] = None  # 图片 URL（支持 base64 data URL）
    file_path: Optional[str] = None  # 文件 VFS 路径（前端上传后）
    file_name: Optional[str] = None  # 原始文件名
    group_id: Optional[str] = None  # 群聊 ID（P0-2 fix：非空时走群聊逻辑）


class SessionResponse(BaseModel):
    """会话响应"""
    session_id: str
    message_count: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    topic: Optional[str] = None
    last_message: Optional[str] = None


class CreateSessionRequest(BaseModel):
    """创建会话请求（Mobile 一对一场景：创建 Agent 后立即创建 Session）"""
    agent_hash: str
    # 可选：会话主题。Mobile 端通常不传，前端按空 topic 显示占位文案。
    topic: Optional[str] = None


class CreateSessionResponse(BaseModel):
    """创建会话响应"""
    session_id: str
    topic: Optional[str] = None
    created_at: Optional[datetime] = None
    agent_hash: str


class MessageResponse(BaseModel):
    """消息响应"""
    role: str
    content: str
    timestamp: Optional[str] = None


class SessionDetailResponse(BaseModel):
    """会话详情响应"""
    session_id: str
    message_count: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    topic: Optional[str] = None
    messages: List[MessageResponse] = []  # Pydantic v2 handles this correctly in model_post_init


# ========== 路由 ==========

@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    agent_hash: Optional[str] = Query(None),
):
    """
    流式聊天 - 调用统一的 ChatService

    发送消息并返回 SSE 流式响应

    请求体:
    {
        "content": "你好",
        "session_id": "sess_xxx",  // 可选
        "image_url": "data:image/png;base64,..."  // 可选，支持 base64
    }

    P0-2 fix: 新增 `group_id` 字段。传 group_id 时走群聊流式逻辑（保存用户消息 + LLM 流式回复 + 保存 Agent 回复）；
    不传时仍走原私聊逻辑（完全不变）。

    SSE 事件:
    - event: token, data: {"content": "..."}
    - event: done, data: {"session_id": "...", "usage": {...}}
    - event: error, data: {"code": "...", "message": "..."}
    """
    import logging
    logger = logging.getLogger(__name__)

    # P0-2: 群聊分支 —— 校验群存在/所有权、保存用户消息、流式生成、回复保存为群消息
    if request.group_id:
        return await _chat_stream_group(
            request=request,
            user_id=user_id,
            db=db,
            agent_hash=agent_hash,
            logger=logger,
        )

    logger.info(f"[chat_stream] user_id={user_id}, content={request.content[:50]}..., image_url={request.image_url[:50] if request.image_url else None}...")

    chat_service = WebChannelService(db, user_id=user_id, agent_hash=agent_hash)

    return StreamingResponse(
        chat_service.chat_stream(request.content, request.session_id, request.image_url, request.file_path, request.file_name),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


async def _chat_stream_group(
    request: "ChatRequest",
    user_id: int,
    db: Session,
    agent_hash: Optional[str],
    logger: logging.Logger,
):
    """
    P0-2: 群聊流式聊天

    - 校验群存在 & 当前用户是群主（owner）
    - 将用户消息保存为 GroupMessage（sender_type="user"）
    - 选取一个 agent（用户默认 agent / 第一个群成员）走私聊式 LLM 流式
    - 把 Agent 的完整回复保存为 GroupMessage（sender_type="agent"）
    - SSE 事件协议与私聊一致（token / thinking / tool / done / error）
    """
    from models.group import Group, GroupMember, GroupMessage
    import uuid as _uuid
    from datetime import datetime as _dt

    group_id = request.group_id
    if not request.content or not request.content.strip():
        raise HTTPException(status_code=400, detail="Message content cannot be empty")

    group = db.query(Group).filter(Group.id == group_id).first()
    if not group or group.deleted_at:
        raise HTTPException(status_code=404, detail="Group not found")
    if group.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this group")

    # 1. 保存用户消息
    user_msg_id = str(_uuid.uuid4())
    user_msg = GroupMessage(
        id=user_msg_id,
        group_id=group_id,
        sender_type="user",
        sender_hash="",
        content=request.content,
        message_type="text",
        attachments=None,
        mentions=[],
        round=0,
        created_at=_dt.utcnow(),
    )
    db.add(user_msg)
    db.commit()
    logger.info(
        f"[chat_stream:group] user={user_id} group={group_id} user_msg={user_msg_id} "
        f"content_len={len(request.content)}"
    )

    # 2. 选 agent：query 上的 agent_hash > 用户默认 agent > 群第一个成员 agent
    selected_agent_hash = agent_hash
    if not selected_agent_hash:
        from models.database import AgentProfile
        default_agent = db.query(AgentProfile).filter(
            AgentProfile.user_id == user_id,
            AgentProfile.status == "initialized",
        ).order_by(AgentProfile.is_default.desc(), AgentProfile.updated_at.desc()).first()
        if default_agent:
            selected_agent_hash = default_agent.hash
    if not selected_agent_hash:
        first_member = db.query(GroupMember).filter(
            GroupMember.group_id == group_id,
            GroupMember.agent_hash != "",
        ).first()
        if first_member:
            selected_agent_hash = first_member.agent_hash
    if not selected_agent_hash:
        # 没有可用 agent —— 把"无 agent"标记保存为系统提示消息
        err_msg = "（群内暂无可用 Agent，无法生成回复）"
        bot_msg = GroupMessage(
            id=str(_uuid.uuid4()),
            group_id=group_id,
            sender_type="agent",
            sender_hash="",
            content=err_msg,
            message_type="text",
            attachments=None,
            mentions=[],
            round=0,
            created_at=_dt.utcnow(),
        )
        db.add(bot_msg)
        db.commit()

        async def _no_agent_gen():
            yield f"event: token\ndata: {json.dumps({'content': err_msg}, ensure_ascii=False)}\n\n"
            yield f"event: done\ndata: {json.dumps({'group_id': group_id, 'usage': {}}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            _no_agent_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    chat_service = WebChannelService(db, user_id=user_id, agent_hash=selected_agent_hash)

    # 3. 流式生成 + 收集完整回复
    full_response = ""

    async def _wrap_group_stream():
        nonlocal full_response
        try:
            async for sse_str in chat_service.chat_stream(
                request.content, request.session_id, request.image_url,
                request.file_path, request.file_name,
            ):
                # 抓 token 内容用于持久化（不动 SSE 原文）
                _evt, _data = _parse_sse(sse_str)
                if _evt == "token" and _data and isinstance(_data.get("content"), str):
                    full_response += _data["content"]
                yield sse_str
        finally:
            # 4. 把 Agent 回复保存为群消息（无论 stream 是否异常退出，能存多少存多少）
            try:
                _bot_msg = GroupMessage(
                    id=str(_uuid.uuid4()),
                    group_id=group_id,
                    sender_type="agent",
                    sender_hash=selected_agent_hash,
                    content=full_response,
                    message_type="text",
                    attachments=None,
                    mentions=[],
                    round=0,
                    created_at=_dt.utcnow(),
                )
                # 用独立 SessionLocal 避免外部 db 已被消费/关闭
                _save_db = SessionLocal()
                try:
                    _save_db.add(_bot_msg)
                    _save_db.commit()
                finally:
                    _save_db.close()
                logger.info(
                    f"[chat_stream:group] saved agent reply group={group_id} "
                    f"agent={selected_agent_hash} len={len(full_response)}"
                )
            except Exception as _save_err:
                logger.error(f"[chat_stream:group] failed to save agent reply: {_save_err}")

    return StreamingResponse(
        _wrap_group_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


def _parse_sse(sse_str: str):
    """解析 SSE 格式字符串，返回 (event_type, data_dict)"""
    lines = sse_str.strip().split("\n")
    event_type = None
    data = None
    for line in lines:
        if line.startswith("event: "):
            event_type = line[7:]
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                data = {}
    return event_type, data


@router.websocket("/api/chat/ws")
async def chat_websocket(websocket: WebSocket):
    """
    WebSocket 流式聊天端点

    连接后发送 JSON:
      {"content": "你好", "session_id": "sess_xxx", "image_url": "data:image/..."}

    接收 JSON 事件:
      {"type": "token", "content": "..."}
      {"type": "thinking", "content": "..."}
      {"type": "tool", "content": "...", "tool_name": "..."}
      {"type": "tool_result", "content": "...", "tool_name": "..."}
      {"type": "done", "session_id": "...", "usage": {...}}
      {"type": "error", "code": "...", "message": "..."}

    认证方式: header Authorization: Bearer xxx 或 query ?token=xxx
    """
    logger = logging.getLogger(__name__)
    await websocket.accept()

    # 记录客户端信息
    try:
        logger.warning(f"[WS_DEBUG] New WS connection from {websocket.client.host}:{websocket.client.port}")
        logger.warning(f"[WS_DEBUG] Headers: {dict(websocket.headers)}")
        logger.warning(f"[WS_DEBUG] Query: {websocket.query_params}")
    except Exception:
        pass

    # JWT 认证
    token = None
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = websocket.query_params.get("token", "")

    if not token:
        await websocket.send_json({"type": "error", "code": "AUTH_ERROR", "message": "未提供认证令牌"})
        await websocket.close(code=4001)
        return

    payload = decode_jwt_token(token)
    if not payload or not payload.get("user_id"):
        await websocket.send_json({"type": "error", "code": "AUTH_ERROR", "message": "令牌无效或已过期"})
        await websocket.close(code=4001)
        return

    user_id: int = payload["user_id"]
    # 从 Host header 提取子域名 agent_hash（必须显式指定）
    host = websocket.headers.get("host", "")
    agent_hash = extract_hash_from_host(host) if host else None
    # 主域名 / 纯 IP 下从 query param 获取 agent_hash
    if not agent_hash:
        agent_hash = websocket.query_params.get("agent_hash") or None
    db = SessionLocal()

    # 🔒 安全校验：验证 user 是否拥有该 agent（防止 subdomain 劫持）
    if not agent_hash:
        await websocket.send_json({"type": "error", "code": "AGENT_REQUIRED", "message": "必须通过 Agent 子域名访问 WebSocket"})
        await websocket.close(code=4004)
        return
    from models.agent_profile import AgentProfile
    agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
    if not agent:
        await websocket.send_json({"type": "error", "code": "AGENT_NOT_FOUND", "message": "Agent 不存在"})
        await websocket.close(code=4004)
        return
    if agent.user_id != user_id:
        await websocket.send_json({"type": "error", "code": "FORBIDDEN", "message": "无权访问此 Agent"})
        await websocket.close(code=4003)
        return

    async def heartbeat():
        """每 30 秒 ping 一次保持连接，首次立即发送"""
        try:
            await websocket.send_json({"type": "ping"})
        except Exception:
            return
        while True:
            try:
                await asyncio.sleep(30)
                logger.warning(f"[WS_DEBUG] Sending ping to user={user_id}")
                await websocket.send_json({"type": "ping"})
            except Exception as e:
                logger.warning(f"[WS_DEBUG] Heartbeat failed for user={user_id}: {e}")
                break

    heartbeat_task = asyncio.create_task(heartbeat())

    chat_service = WebChannelService(db, user_id=user_id, agent_hash=agent_hash)

    try:
        while True:
            # 接收消息
            try:
                logger.warning(f"[WS_DEBUG] Waiting for message from user={user_id}...")
                data = await websocket.receive_json()
                logger.warning(f"[WS_DEBUG] Received message from user={user_id}: keys={list(data.keys())}, content_len={len(data.get('content','') or '')}, session_id={data.get('session_id')}")
            except WebSocketDisconnect:
                logger.warning(f"[WS_DEBUG] user={user_id} disconnected (WebSocketDisconnect on receive)")
                break

            content = data.get("content", "")
            session_id = data.get("session_id")
            image_url = data.get("image_url")
            file_path = data.get("file_path")
            file_name = data.get("file_name")

            if not content and not image_url and not file_path:
                await websocket.send_json({"type": "error", "code": "EMPTY_INPUT", "message": "消息内容为空"})
                continue

            logger.info(f"[WS] user_id={user_id}, content={content[:50] if content else ''}..., image_url={'yes' if image_url else 'no'}, file={'yes' if file_path else 'no'}")

            # 路由层拦截：开启新会话（在已有对话中插入分割线 + 招呼）
            _new_session_cmds = {"开启新会话", "新对话", "新会话", "重新开始", "结束对话", "结束会话", "开启新对话"}
            if content in _new_session_cmds:
                # 发送分割线标记
                await websocket.send_json({"type": "divider", "content": "──── 新对话 ────"})
                # 读取 session memory 生成招呼
                # 读取 session memory，用 LLM 生成打招呼
                _greeting = ""
                _memory = ""
                try:
                    from services.virtual_filesystem import VirtualFileSystem
                    _vfs = VirtualFileSystem(agent_hash=chat_service.agent_hash)
                    _memory = await asyncio.to_thread(_vfs.cat, "/workspace/agent/session_memory.md")
                except Exception as _e:
                    logger.warning(f"[WS] session memory read error: {_e}")

                if _memory and not _memory.startswith("Error"):
                    try:
                        from services.llm_service import LLMService
                        from services.model_registry import resolve as _resolve
                        _cfg = _resolve(settings.MAIN_TEXT_MODEL)
                        _llm = LLMService()
                        async for chunk in _llm.chat(
                            messages=[{
                                "role": "system",
                                "content": "根据对话记忆生成一句自然的打招呼消息。"
                                           "用 2-4 句话表达还记得对方并邀请继续对话。"
                                           "不要用「欢迎回来」「我记得」「很开心见到你」等生硬的表述。"
                            }, {
                                "role": "user",
                                "content": f"对话记忆：\n{_memory.strip()}"
                            }],
                            provider=_cfg["provider"],
                            model=settings.MAIN_TEXT_MODEL,
                        ):
                            _greeting += chunk
                    except Exception as _e:
                        logger.warning(f"[WS] LLM greeting error: {_e}")
                else:
                    # 没有 session memory，尝试用 Agent 人格生成招呼
                    _greeting = ""
                    try:
                        _identity = await asyncio.to_thread(_vfs.cat, "/workspace/agent/identity.md")
                        _soul = await asyncio.to_thread(_vfs.cat, "/workspace/agent/soul.md")
                        _persona = ""
                        if _identity and not _identity.startswith("Error"):
                            _persona += f"身份配置：\n{_identity.strip()}\n\n"
                        if _soul and not _soul.startswith("Error"):
                            _persona += f"人格设定：\n{_soul.strip()}"
                        if _persona:
                            from services.llm_service import LLMService
                            from services.model_registry import resolve as _resolve
                            _cfg = _resolve(settings.MAIN_TEXT_MODEL)
                            _llm = LLMService()
                            async for chunk in _llm.chat(
                                messages=[{
                                    "role": "system",
                                    "content": "根据 AI 助手的身份和人格设定，生成一句自然的开场打招呼消息。"
                                               "用 1-2 句话表达热情和欢迎。"
                                               "语气要符合人格设定（活泼/元气/温柔等），不要用「你好呀」等太通用的开场。"
                                               "直接以助手的身份说话，不要提及「这是一个开场白」之类的元描述。"
                                }, {
                                    "role": "user",
                                    "content": f"这是 AI 助手的人格设定：\n\n{_persona}"
                                }],
                                provider=_cfg["provider"],
                                model=settings.MAIN_TEXT_MODEL,
                            ):
                                _greeting += chunk
                    except Exception as _e:
                        logger.warning(f"[WS] personality greeting error: {_e}")

                if not _greeting:
                    _greeting = "你好呀！有什么想聊的吗？"
                await websocket.send_json({"type": "token", "content": _greeting})
                await websocket.send_json({"type": "done", "session_id": session_id, "usage": {"input_tokens": 0, "output_tokens": 0}})
                continue

            try:
                async for sse_str in chat_service.chat_stream(content, session_id, image_url, file_path, file_name):
                    event_type, event_data = _parse_sse(sse_str)
                    if event_type:
                        payload = {"type": event_type}
                        if event_data:
                            payload.update(event_data)
                        try:
                            await websocket.send_json(payload)
                        except Exception:
                            # 连接已断开
                            return
            except Exception as e:
                logger.error(f"[WS] chat_stream error: {e}")
                try:
                    await websocket.send_json({"type": "error", "code": "STREAM_ERROR", "message": str(e)})
                except Exception:
                    pass

    except WebSocketDisconnect:
        logger.warning(f"[WS_DEBUG] user={user_id} disconnected during processing (outer WebSocketDisconnect)")
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        db.close()
        logger.warning(f"[WS_DEBUG] user={user_id} connection closed (finally)")


@router.post("/api/chat/sessions", response_model=CreateSessionResponse)
async def create_chat_session(
    request: CreateSessionRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
) -> CreateSessionResponse:
    """
    创建一个新会话（Mobile 一对一场景）。

    - 校验 `agent_hash` 存在且属于当前用户
    - 创建一个 `ConversationSession`：`topic` 设为 `[mobile]`（保留渠道前缀），
      不带具体 topic 文本（Mobile 端在 topic 为空时显示"新创建的 AI 向导"占位）
    - 立即返回新会话 id，调用方可直接 navigate 到聊天页
    """
    from models.database import AgentProfile, ConversationSession
    from services.web_channel_service import CHANNEL_MOBILE
    import uuid as _uuid

    agent_hash = (request.agent_hash or "").strip()
    if not agent_hash:
        raise HTTPException(status_code=400, detail="agent_hash is required")

    # 校验 Agent 存在且属于当前用户
    agent = db.query(AgentProfile).filter(AgentProfile.hash == agent_hash).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this agent")

    # 创建新会话（topic 保留渠道前缀，主题内容留空 → 客户端走占位文案）
    new_session_id = f"sess_{_uuid.uuid4().hex[:16]}"
    now = datetime.utcnow()
    session = ConversationSession(
        session_id=new_session_id,
        agent_hash=agent_hash,
        user_id=user_id,
        messages="[]",
        created_at=now,
        updated_at=now,
        message_count=0,
        token_count=0,
        is_archived=False,
    )
    # 渠道信息存在 topic 字段（与 Web/WeChat 一致）
    session.topic = f"[{CHANNEL_MOBILE}]" if not request.topic else f"[{CHANNEL_MOBILE}]{request.topic}"
    db.add(session)
    db.commit()
    db.refresh(session)

    return CreateSessionResponse(
        session_id=session.session_id,
        topic=session.topic,
        created_at=session.created_at,
        agent_hash=agent_hash,
    )


@router.get("/api/chat/sessions", response_model=List[SessionResponse])
async def get_sessions(
    agent_hash: Optional[str] = Query(None, description="Agent hash，筛选特定 Agent 的会话"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    获取会话列表

    返回用户的 Web 端 + Mobile 端会话。渠道通过 topic 前缀区分（[web] / [mobile]），
    列表渲染时按 topic 携带的真实文案展示。
    可选 agent_hash 参数筛选特定 Agent 的会话。
    """
    from models.database import ConversationSession
    from sqlalchemy import or_

    # 一次性查回 [web] 和 [mobile] 渠道的会话（避免两次往返 + 各自 sort）
    q = db.query(ConversationSession).filter(
        ConversationSession.user_id == user_id,
        ConversationSession.is_archived == False  # noqa: E712
    ).filter(
        or_(
            ConversationSession.topic.like("[web]%"),
            ConversationSession.topic.like("[mobile]%"),
        )
    )
    if agent_hash:
        q = q.filter(ConversationSession.agent_hash == agent_hash)
    rows = q.order_by(
        ConversationSession.updated_at.desc()
    ).limit(50).all()

    result: List[SessionResponse] = []
    for s in rows:
        # 解析 messages 拿首条 user 消息
        first_message = ""
        try:
            msgs = json.loads(s.messages or "[]")
            for m in msgs:
                if m.get("role") == "user":
                    first_message = (m.get("content") or "")[:50]
                    break
        except Exception:
            first_message = ""
        # topic 字段：保留渠道前缀（"[web]" / "[mobile]"）让前端按原值展示
        result.append(
            SessionResponse(
                session_id=s.session_id,
                message_count=s.message_count or 0,
                created_at=s.created_at,
                updated_at=s.updated_at,
                topic=s.topic or "[web]",
                last_message=first_message,
            )
        )
    return result


@router.get("/api/chat/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session_detail(
    session_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    获取会话详情（包含消息历史）

    支持 Web 和 Mobile 两个渠道的会话。Mobile 渠道的会话不再走
    WebChannelService.get_session 那一套（被 topic 过滤掉了），这里直接查 ORM。
    """
    from models.database import ConversationSession

    # 直接查 ORM（避免 web_channel_service 内部只查 [web] 渠道的过滤）
    s = (
        db.query(ConversationSession)
        .filter(
            ConversationSession.session_id == session_id,
            ConversationSession.user_id == user_id,
        )
        .first()
    )
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")

    # 解析 messages
    try:
        messages_raw = json.loads(s.messages or "[]")
    except Exception:
        messages_raw = []

    return SessionDetailResponse(
        session_id=s.session_id,
        message_count=s.message_count or 0,
        created_at=s.created_at,
        updated_at=s.updated_at,
        topic=s.topic or "[web]",
        messages=[
            MessageResponse(
                role=msg.get("role", "user"),
                content=msg.get("content", ""),
                timestamp=msg.get("timestamp"),
            )
            for msg in messages_raw
        ],
    )


@router.delete("/api/chat/sessions/{session_id}")
async def archive_session(
    session_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
) -> dict:
    """
    归档会话
    """
    service = WebChannelService(db, user_id)
    success = service.archive_session(session_id)

    if not success:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"status": "ok", "message": "Session archived"}
