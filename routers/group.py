"""
Group Chat REST API - Phase 4 Engine
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from models.database import get_db, AgentProfile
from models.group import Group, GroupMember, GroupMessage
from utils.auth import get_current_user_id
from services.group_service import group_dispatch_service, GroupDispatchService
from config import settings

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