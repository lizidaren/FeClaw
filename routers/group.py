"""
Group Chat REST API - Phase 4 Engine
"""

import asyncio
import json
import logging
import time
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from models.database import get_db, AgentProfile, SessionLocal
from models.group import Group, GroupMember, GroupMessage
from utils.auth import get_current_user_id
from services.group_service import group_dispatch_service, GroupDispatchService
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/groups", tags=["Groups"])


# ==========================================
# Request / Response Models
# ==========================================

class CreateGroupRequest(BaseModel):
    name: str
    member_hashes: Optional[List[str]] = []
    settings: Optional[dict] = None
    context_isolation: bool = True
    max_rounds: int = 100


class UpdateGroupRequest(BaseModel):
    name: Optional[str] = None
    announcement: Optional[str] = None
    settings: Optional[dict] = None
    context_isolation: Optional[bool] = None
    max_rounds: Optional[int] = None


class AddMemberRequest(BaseModel):
    agent_hash: str
    role: str = "member"


class SendMessageRequest(BaseModel):
    content: str
    mentions: Optional[List[str]] = None
    attachments: Optional[List[dict]] = None
    message_type: str = "text"


class GroupResponse(BaseModel):
    id: str
    name: str
    announcement: str
    announcement_updated_at: Optional[int] = None
    owner_user_id: int
    settings: dict
    context_isolation: bool
    max_rounds: int
    created_at: int
    member_count: int = 0


class MemberResponse(BaseModel):
    agent_hash: str
    role: str
    is_silent: bool
    joined_at: int


class MessageResponse(BaseModel):
    id: str
    sender_type: str
    sender_hash: Optional[str]
    content: str
    message_type: str
    attachments: Optional[List[dict]]
    mentions: List[str]
    round: int
    created_at: int


# ==========================================
# Helpers
# ==========================================

def _get_group_or_404(db: Session, group_id: str, user_id: int) -> Group:
    """Verify group exists and user owns it (or is member via agent)."""
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group or group.deleted_at:
        raise HTTPException(status_code=404, detail="Group not found")
    if group.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this group")
    return group


def _format_group(db: Session, group: Group) -> GroupResponse:
    """Format a Group DB model into API response."""
    member_count = db.query(GroupMember).filter(GroupMember.group_id == group.id).count()
    return GroupResponse(
        id=group.id,
        name=group.name,
        announcement=group.announcement or "",
        announcement_updated_at=int(group.announcement_updated_at.timestamp()) if group.announcement_updated_at else None,
        owner_user_id=group.owner_user_id,
        settings=group.settings or {},
        context_isolation=group.context_isolation,
        max_rounds=group.max_rounds,
        created_at=int(group.created_at.timestamp()),
        member_count=member_count,
    )


def _format_member(member: GroupMember) -> MemberResponse:
    return MemberResponse(
        agent_hash=member.agent_hash,
        role=member.role,
        is_silent=member.is_silent,
        joined_at=int(member.joined_at.timestamp()),
    )


def _format_message(msg: GroupMessage) -> MessageResponse:
    return MessageResponse(
        id=msg.id,
        sender_type=msg.sender_type,
        sender_hash=msg.sender_hash,
        content=msg.content or "",
        message_type=msg.message_type,
        attachments=msg.attachments,
        mentions=msg.mentions or [],
        round=msg.round,
        created_at=int(msg.created_at.timestamp()),
    )


# ==========================================
# Routes
# ==========================================

@router.post("", response_model=GroupResponse)
async def create_group(
    body: CreateGroupRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Create a new group chat.

    The creating user becomes the owner. Optionally add agent members at creation time.
    """
    if not body.name or len(body.name) > 100:
        raise HTTPException(status_code=400, detail="Group name must be 1-100 characters")

    # Validate member hashes
    if body.member_hashes:
        for h in body.member_hashes:
            agent = db.query(AgentProfile).filter(
                AgentProfile.hash == h,
                AgentProfile.user_id == user_id,
            ).first()
            if not agent:
                raise HTTPException(status_code=400, detail=f"Agent {h} not found or not owned by you")

    svc = GroupDispatchService()
    group = svc.create_group(
        db=db,
        name=body.name,
        owner_user_id=user_id,
        member_hashes=body.member_hashes,
        settings=body.settings,
    )

    return _format_group(db, group)


@router.get("", response_model=List[GroupResponse])
async def list_groups(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    List all groups the current user owns or is a member of.

    Returns groups where user is owner.
    """
    groups = group_dispatch_service.list_user_groups(db, user_id)
    return [_format_group(db, g) for g in groups]


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Get a group by ID."""
    group = _get_group_or_404(db, group_id, user_id)
    return _format_group(db, group)


@router.patch("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: str,
    body: UpdateGroupRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Update group settings."""
    group = _get_group_or_404(db, group_id, user_id)

    if body.name is not None:
        if len(body.name) > 100:
            raise HTTPException(status_code=400, detail="Group name must be 1-100 characters")
        group.name = body.name
    if body.announcement is not None:
        group.announcement = body.announcement
        group.announcement_updated_at = datetime.utcnow()
    if body.settings is not None:
        group.settings = body.settings
    if body.context_isolation is not None:
        group.context_isolation = body.context_isolation
    if body.max_rounds is not None:
        group.max_rounds = body.max_rounds

    group.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(group)

    return _format_group(db, group)


@router.delete("/{group_id}")
async def delete_group(
    group_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Soft-delete a group (owner only)."""
    group = _get_group_or_404(db, group_id, user_id)
    if group.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the owner can delete the group")

    group.deleted_at = datetime.utcnow()
    db.commit()

    return JSONResponse(content={"status": "ok", "group_id": group_id})


# ==========================================
# Members
# ==========================================

@router.get("/{group_id}/members", response_model=List[MemberResponse])
async def list_members(
    group_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """List all members of a group."""
    _get_group_or_404(db, group_id, user_id)

    members = db.query(GroupMember).filter(GroupMember.group_id == group_id).all()
    return [_format_member(m) for m in members]


@router.post("/{group_id}/members", response_model=MemberResponse)
async def add_member(
    group_id: str,
    body: AddMemberRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Add an agent member to a group (owner only)."""
    group = _get_group_or_404(db, group_id, user_id)
    if group.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the owner can add members")

    # Verify agent exists and belongs to user
    agent = db.query(AgentProfile).filter(
        AgentProfile.hash == body.agent_hash,
        AgentProfile.user_id == user_id,
    ).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found or not owned by you")

    svc = GroupDispatchService()
    member = svc.add_member(db, group_id, body.agent_hash, role=body.role)
    return _format_member(member)


@router.delete("/{group_id}/members/{agent_hash}")
async def remove_member(
    group_id: str,
    agent_hash: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Remove an agent member from a group (owner only)."""
    group = _get_group_or_404(db, group_id, user_id)
    if group.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the owner can remove members")

    svc = GroupDispatchService()
    ok = svc.remove_member(db, group_id, agent_hash)
    if not ok:
        raise HTTPException(status_code=404, detail="Member not found")

    return JSONResponse(content={"status": "ok"})


# ==========================================
# Messages
# ==========================================

@router.get("/{group_id}/messages", response_model=List[MessageResponse])
async def get_messages(
    group_id: str,
    before: Optional[int] = Query(None, description="Unix timestamp — return messages before this time"),
    limit: int = Query(50, ge=1, le=200),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Get group message history (newest first)."""
    _get_group_or_404(db, group_id, user_id)

    before_dt = datetime.fromtimestamp(before) if before else None
    svc = GroupDispatchService()
    messages = svc.get_messages(db, group_id, before=before_dt, limit=limit)
    # get_messages returns newest-first; API should be newest-first
    return [_format_message(m) for m in reversed(messages)]


@router.post("/{group_id}/messages", response_model=dict)
async def send_message(
    group_id: str,
    body: SendMessageRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Send a message to a group (from user channel).

    This triggers the group dispatch: all agent members will be notified
    and may reply based on their wake conditions.
    """
    group = _get_group_or_404(db, group_id, user_id)

    if not body.content or not body.content.strip():
        raise HTTPException(status_code=400, detail="Message content cannot be empty")

    msg_id = await group_dispatch_service.on_message(
        group_id=group_id,
        sender_type="user",
        sender_hash="",
        content=body.content,
        mentions=body.mentions,
        attachments=body.attachments,
        message_type=body.message_type,
    )

    return JSONResponse(content={"status": "ok", "msg_id": msg_id})


# ==========================================
# SSE Stream — B1 (R 代理 2026-07-18)
# ==========================================
#
# 设计目标：解决 classic agent fire-and-forget 异步 dispatch 的回复
# 前端收不到的问题。前端在 sendGroupMessage 成功后调用本端点，长连
# 接（≤ 5 min）持续收 agent 回复；断开即自动重连。
#
# 协议：
#   event: message  → data: <json MessageResponse>  (新消息)
#   event: ping     → data: {}                       (心跳，5s 一次)
#   event: done     → data: {"last_id": "..."}       (5 min 到时或客户端断)
#
# 鉴权：owner 或群成员（其名下 agent 在群内）均可订阅。比 send_message
# 的 owner-only 更宽松，便于多端订阅同一群。

@router.get("/{group_id}/stream")
async def stream_group_messages(
    group_id: str,
    after: Optional[str] = Query(None, description="仅返严格晚于此 message_id 的新消息；缺省=该群最新 limit 条"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """SSE 订阅群消息 — fire-and-forget agent 回复也能收到。"""
    svc = GroupDispatchService()
    if not svc.is_member_or_owner(db, group_id, user_id):
        raise HTTPException(status_code=403, detail="not a member of this group")

    max_duration = 300  # 5 min
    heartbeat_every = 5  # 每 5 次空轮询发一次 ping
    poll_interval = 1.0

    async def event_generator():
        # 长轮询要自管 DB session —— Depends 注入的 db 会在响应后关闭。
        poll_db = SessionLocal()
        last_id = after or ""
        start = time.monotonic()
        idle_count = 0
        try:
            while time.monotonic() - start < max_duration:
                try:
                    new_messages = svc.get_new_messages_since(
                        poll_db, group_id, last_id, limit=50
                    )
                except Exception as e:
                    logger.warning(
                        f"[GroupStream] poll error group={group_id} "
                        f"user={user_id}: {e}"
                    )
                    yield f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(poll_interval)
                    continue

                if new_messages:
                    for msg in new_messages:
                        payload = {
                            "id": msg.id,
                            "sender_type": msg.sender_type,
                            "sender_hash": msg.sender_hash,
                            "content": msg.content or "",
                            "message_type": msg.message_type,
                            "attachments": msg.attachments,
                            "mentions": msg.mentions or [],
                            "round": msg.round,
                            "created_at": int(msg.created_at.timestamp()),
                        }
                        yield (
                            f"event: message\n"
                            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        )
                        last_id = msg.id
                    idle_count = 0
                else:
                    idle_count += 1
                    if idle_count >= heartbeat_every:
                        yield f"event: ping\ndata: {json.dumps({})}\n\n"
                        idle_count = 0

                await asyncio.sleep(poll_interval)

            yield f"event: done\ndata: {json.dumps({'last_id': last_id}, ensure_ascii=False)}\n\n"
        finally:
            try:
                poll_db.close()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ==========================================
# Moments
# ==========================================

class MomentResponse(BaseModel):
    id: str
    group_id: str
    agent_hash: Optional[str]
    kind: str
    title: Optional[str]
    content: Optional[str]
    attachments: List[dict]
    created_at: int


def _format_moment(moment) -> MomentResponse:
    return MomentResponse(
        id=moment.id,
        group_id=moment.group_id,
        agent_hash=moment.agent_hash,
        kind=moment.kind,
        title=moment.title,
        content=moment.content,
        attachments=moment.attachments or [],
        created_at=int(moment.created_at.timestamp()),
    )


@router.get("/{group_id}/moments", response_model=List[MomentResponse])
async def list_group_moments(
    group_id: str,
    before: Optional[int] = Query(None, description="Unix timestamp — return moments before this time"),
    limit: int = Query(50, ge=1, le=200),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """List moments for a specific group (newest first)."""
    _get_group_or_404(db, group_id, user_id)

    before_dt = datetime.fromtimestamp(before) if before else None
    from services.moments_service import moments_service
    moments = moments_service.get_moments(db, group_id, before=before_dt, limit=limit)
    return [_format_moment(m) for m in moments]


@router.delete("/{group_id}/moments/{moment_id}")
async def delete_group_moment(
    group_id: str,
    moment_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Delete a moment (owner of the group only)."""
    _get_group_or_404(db, group_id, user_id)

    from services.moments_service import moments_service
    ok = moments_service.delete_moment(db, moment_id, group_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Moment not found or not authorized")

    return JSONResponse(content={"status": "ok"})