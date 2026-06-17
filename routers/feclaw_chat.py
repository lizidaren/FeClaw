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
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from sqlalchemy.orm import Session
from models.database import SessionLocal
from config import settings
from routers.feclaw_domain import extract_hash_from_host
from services.web_channel_service import WebChannelService
from utils.auth import get_current_user_id, decode_jwt_token


# 数据库依赖
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

router = APIRouter(tags=["FeClaw Chat"])


# ========== 请求模型 ==========

class ChatRequest(BaseModel):
    """聊天请求"""
    content: str
    session_id: Optional[str] = None
    image_url: Optional[str] = None  # 图片 URL（支持 base64 data URL）


class SessionResponse(BaseModel):
    """会话响应"""
    session_id: str
    message_count: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    topic: Optional[str] = None
    last_message: Optional[str] = None


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
    db: Session = Depends(get_db)
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
    
    SSE 事件:
    - event: token, data: {"content": "..."}
    - event: done, data: {"session_id": "...", "usage": {...}}
    - event: error, data: {"code": "...", "message": "..."}
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"[chat_stream] user_id={user_id}, content={request.content[:50]}..., image_url={request.image_url[:50] if request.image_url else None}...")
    
    chat_service = WebChannelService(db, user_id=user_id)

    return StreamingResponse(
        chat_service.chat_stream(request.content, request.session_id, request.image_url),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
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
    # 从 Host header 提取子域名 agent_hash
    host = websocket.headers.get("host", "")
    agent_hash = extract_hash_from_host(host) if host else None
    db = SessionLocal()

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

            if not content and not image_url:
                await websocket.send_json({"type": "error", "code": "EMPTY_INPUT", "message": "消息内容为空"})
                continue

            logger.info(f"[WS] user_id={user_id}, content={content[:50] if content else ''}..., image_url={'yes' if image_url else 'no'}")

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
                async for sse_str in chat_service.chat_stream(content, session_id, image_url):
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


@router.get("/api/chat/sessions", response_model=List[SessionResponse])
async def get_sessions(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    获取会话列表
    
    返回用户的所有 Web 端会话
    """
    chat_service = WebChannelService(db, user_id=user_id)
    sessions = chat_service.get_session_list(limit=50)
    
    result = []
    for session in sessions:
        result.append(SessionResponse(
            session_id=session["session_id"],
            message_count=session["message_count"],
            created_at=datetime.fromisoformat(session["created_at"]) if session.get("created_at") else None,
            updated_at=datetime.fromisoformat(session["updated_at"]) if session.get("updated_at") else None,
            topic="[web]",
            last_message=session.get("first_message")
        ))
    
    return result


@router.get("/api/chat/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session_detail(
    session_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db)
):
    """
    获取会话详情（包含消息历史）
    """
    chat_service = WebChannelService(db, user_id=user_id)

    # 获取会话
    session = chat_service.get_session(session_id)
    if not session:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Session not found")
    
    # 获取消息历史
    messages = session.get("messages", [])
    
    return SessionDetailResponse(
        session_id=session["session_id"],
        message_count=session["message_count"],
        created_at=datetime.fromisoformat(session["created_at"]) if session.get("created_at") else None,
        updated_at=datetime.fromisoformat(session["updated_at"]) if session.get("updated_at") else None,
        topic="[web]",
        messages=[
            MessageResponse(
                role=msg["role"],
                content=msg["content"],
                timestamp=msg.get("timestamp")
            )
            for msg in messages
        ]
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
