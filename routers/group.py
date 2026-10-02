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
from services.vfs.paths import GROUP_ATTACH_DIR, GROUP_SHARE_DIR, GROUP_REF_DIR
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
    # P1.x: 用户拉 Agent 进群时填写的"工作描述"，决定 Agent 在群里的行为模式
    job_description: Optional[str] = None


class SendMessageRequest(BaseModel):
    content: str
    mentions: Optional[List[str]] = None
    attachments: Optional[List[dict]] = None
    message_type: str = "text"
    # P1.x: 是否以共享文件发送。False=走 .attach/{date}/（只读附件，默认），
    # True=走 .share/（活文档，可编辑）
    share_mode: bool = False


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
    # P1.x: 群成员扩展字段（job_description/status/allow_dm）
    job_description: Optional[str] = None
    status: str = "dormant"
    allow_dm: bool = True


class MessageResponse(BaseModel):
    id: str
    group_id: str
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
        job_description=member.job_description,
        status=member.status or "dormant",
        allow_dm=bool(member.allow_dm) if member.allow_dm is not None else True,
    )


def _format_message(msg: GroupMessage) -> MessageResponse:
    return MessageResponse(
        id=msg.id,
        group_id=msg.group_id,
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
    member = svc.add_member(
        db, group_id, body.agent_hash,
        role=body.role,
        job_description=body.job_description,
    )
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

    附件分流逻辑（P1.x）：
    - share_mode=False（默认）：附件走 .attach/{YYYY-MM-DD}/{filename}，只读
    - share_mode=True：附件走 .share/{filename}，活文档可编辑
    实际的 COS 上传由 routers/upload.py 负责；这里只为 attachment 记录分配 COS 路径。
    """
    group = _get_group_or_404(db, group_id, user_id)

    if not body.content or not body.content.strip():
        raise HTTPException(status_code=400, detail="Message content cannot be empty")

    # P1.x: 附件分流 —— 给每个 attachment 分配 COS 路径前缀
    processed_attachments = body.attachments
    if body.attachments:
        processed_attachments = []
        for att in body.attachments:
            filename = att.get("filename") or att.get("name") or "unknown"
            if body.share_mode:
                # .share/ —— 活文档，可编辑
                cos_path = f"feclaw/groups/{group_id}/{GROUP_SHARE_DIR}/{filename}"
                scope = "share"
            else:
                # .attach/{date}/ —— 聊天附件，只读不可变，按日期分组
                today = datetime.utcnow().strftime("%Y-%m-%d")
                cos_path = f"feclaw/groups/{group_id}/{GROUP_ATTACH_DIR}/{today}/{filename}"
                scope = "attach"
            att_copy = dict(att)
            att_copy["cos_path"] = cos_path
            att_copy["scope"] = scope
            processed_attachments.append(att_copy)

    msg_id = await group_dispatch_service.on_message(
        group_id=group_id,
        sender_type="user",
        sender_hash="",
        content=body.content,
        mentions=body.mentions,
        attachments=processed_attachments,
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
# Files — P1.x 群共享空间
# ==========================================

@router.get("/{group_id}/files")
async def list_group_files(
    group_id: str,
    scope: str = Query("share", pattern="^(share|ref|attach)$"),
    tag: Optional[str] = Query(None, description="按标签过滤（后续实现，当前可返回所有）"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """列出群共享空间文件

    - scope=share（默认）：.share/ 活文档
    - scope=ref：.ref/ 参考库（Agent 策展的知识）
    - scope=attach：.attach/ 附件归档
    - tag：按标签过滤（后续实现，当前可返回所有）

    对齐 file_ops.py 中已有的 feclaw/groups/{gid}/ 前缀；
    权限：群主可访问全部 scope；群成员仅可访问 share/attach。
    """
    group = db.query(Group).filter(
        Group.id == group_id,
        Group.deleted_at.is_(None),
    ).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    # 权限：群主可访问任何 scope；其他用户需为群内 agent 持有者
    if group.owner_user_id != user_id:
        from models.group import GroupMember
        owned_agent = db.query(GroupMember).join(
            AgentProfile, AgentProfile.hash == GroupMember.agent_hash
        ).filter(
            GroupMember.group_id == group_id,
            GroupMember.agent_hash != "",
            AgentProfile.user_id == user_id,
        ).first()
        if not owned_agent:
            raise HTTPException(status_code=403, detail="Not authorized to access this group's files")

    scope_to_dir = {
        "share": GROUP_SHARE_DIR,
        "ref": GROUP_REF_DIR,
        "attach": GROUP_ATTACH_DIR,
    }
    cos_prefix = f"feclaw/groups/{group_id}/{scope_to_dir[scope]}/"

    try:
        from services.file_storage import create_file_storage
        storage = create_file_storage()
        objects = storage.list_objects(cos_prefix)
    except Exception as e:
        logger.warning(f"[GroupFiles] list_objects failed group={group_id} scope={scope}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list files: {e}")

    if not objects:
        return {"files": [], "count": 0, "scope": scope, "group_id": group_id}

    files = []
    for obj in objects:
        key = obj.get("Key", "")
        name = key.rsplit("/", 1)[-1] if "/" in key else key
        if not name or name == ".directory":
            continue
        files.append({
            "name": name,
            "key": key,
            "size": obj.get("Size", 0),
            "mtime": obj.get("LastModified", ""),
            "scope": scope,
        })

    return {"files": files, "count": len(files), "scope": scope, "group_id": group_id}


@router.get("/{group_id}/files/download")
async def download_group_file(
    group_id: str,
    key: str = Query(..., description="COS 对象 key，如 feclaw/groups/1/.share/foo.md"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """返回共享文件内容的 presigned GET URL（1 小时有效）。

    安全约束：
    - 只允许访问本群目录下的对象（key 必须以 `feclaw/groups/{group_id}/` 开头）
    - 不允许 .. 穿越
    - 调用方必须是群主或群内 agent 的持有者
    """
    # 1. 权限校验：复用 list 的逻辑（群主 / 群内 agent 持有者）
    group = db.query(Group).filter(
        Group.id == group_id,
        Group.deleted_at.is_(None),
    ).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group.owner_user_id != user_id:
        owned_agent = db.query(GroupMember).join(
            AgentProfile, AgentProfile.hash == GroupMember.agent_hash
        ).filter(
            GroupMember.group_id == group_id,
            GroupMember.agent_hash != "",
            AgentProfile.user_id == user_id,
        ).first()
        if not owned_agent:
            raise HTTPException(status_code=403, detail="Not authorized to access this group's files")

    # 2. Key 校验：必须落在本群目录下
    expected_prefix = f"feclaw/groups/{group_id}/"
    if not key.startswith(expected_prefix):
        raise HTTPException(
            status_code=400,
            detail=f"key 必须以 {expected_prefix!r} 开头，防止跨群访问",
        )
    if ".." in key:
        raise HTTPException(status_code=400, detail="key 不允许 .. 穿越")

    # 3. 生成 presigned GET URL（1 小时）
    try:
        from services.storage_service import CosStorage
        from config import settings
        cos_url = (
            f"https://{settings.TENCENT_COS_BUCKET}"
            f".cos.{settings.TENCENT_COS_REGION}.myqcloud.com/{key}"
        )
        storage = CosStorage()
        presigned_get_url = storage.generate_presigned_get_url(cos_url, expired=3600)
    except Exception as e:
        logger.warning(f"[GroupFiles] generate_presigned_get_url failed key={key}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate download URL: {e}")

    return {"url": presigned_get_url, "key": key, "expires_in": 3600}


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