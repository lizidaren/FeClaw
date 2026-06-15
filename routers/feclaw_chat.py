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
    db = SessionLocal()

    async def heartbeat():
        """每 30 秒 ping 一次保持连接"""
        while True:
            try:
                await asyncio.sleep(30)
                await websocket.send_json({"type": "ping"})
            except Exception:
                break

    heartbeat_task = asyncio.create_task(heartbeat())

    try:
        while True:
            # 接收消息
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                logger.info(f"[WS] user={user_id} disconnected")
                break

            content = data.get("content", "")
            session_id = data.get("session_id")
            image_url = data.get("image_url")

            if not content and not image_url:
                await websocket.send_json({"type": "error", "code": "EMPTY_INPUT", "message": "消息内容为空"})
                continue

            logger.info(f"[WS] user_id={user_id}, content={content[:50] if content else ''}..., image_url={'yes' if image_url else 'no'}")

            chat_service = WebChannelService(db, user_id=user_id)

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
        logger.info(f"[WS] user={user_id} disconnected during processing")
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        db.close()


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
