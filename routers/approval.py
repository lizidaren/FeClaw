"""
Approval REST API — Phase 4

提供审批同意/拒绝端点。前端从 GroupMessage.message_type=approval_card
读出 request_id 后，调用这些端点。

POST /api/approvals/{request_id}/grant
POST /api/approvals/{request_id}/deny
GET  /api/approvals/{request_id}        — 查询当前状态
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db
from models.group import Group
from services.approval_service import ApprovalService, ApprovalStatus
from services.tools.permission_tools import (
    PermissionToolsMixin,
    grant_and_execute,
)
from utils.auth import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/approvals", tags=["Approvals"])


# ──────────── Request / Response ────────────


class GrantRequest(BaseModel):
    decided_by: Optional[str] = None  # 默认用当前 user_id


class DenyRequest(BaseModel):
    decided_by: Optional[str] = None
    reason: Optional[str] = None


class ApprovalResponse(BaseModel):
    id: str
    agent_hash: str
    action: str
    target: str
    intent: str
    file_path: str
    file_scope: str
    status: str
    created_at: float
    expires_at: float
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    error: Optional[str] = None
    checksum_match: Optional[bool] = None  # 仅 grant 时返回


# ──────────── Helpers ────────────


def _user_can_decide(req, user_id: int, db: Session) -> None:
    """校验用户能否审批此请求。

    - target='group:{id}'  → 必须是群主
    - target='user:{id}'   → 必须是该 user
    """
    if req.target.startswith("group:"):
        gid = int(req.target.split(":", 1)[1])
        group = (
            db.query(Group)
            .filter(Group.id == gid, Group.deleted_at.is_(None))
            .first()
        )
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        if group.owner_user_id != user_id:
            raise HTTPException(status_code=403, detail="Only the group owner can decide")
    elif req.target.startswith("user:"):
        uid = int(req.target.split(":", 1)[1])
        if uid != user_id:
            raise HTTPException(status_code=403, detail="Not your approval")
    else:
        raise HTTPException(status_code=400, detail="Invalid request target")


def _req_to_response(req, checksum_match: Optional[bool] = None) -> ApprovalResponse:
    return ApprovalResponse(
        id=req.id,
        agent_hash=req.agent_hash,
        action=req.action,
        target=req.target,
        intent=req.intent,
        file_path=req.file_path,
        file_scope=req.file_scope,
        status=req.status,
        created_at=req.created_at,
        expires_at=req.expires_at,
        decided_at=req.decided_at,
        decided_by=req.decided_by,
        error=req.error,
        checksum_match=checksum_match,
    )


# ──────────── Routes ────────────


@router.get("/{request_id}", response_model=ApprovalResponse)
async def get_approval(
    request_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """查询审批请求当前状态。"""
    req = ApprovalService.instance().get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    _user_can_decide(req, user_id, db)
    return _req_to_response(req)


@router.post("/{request_id}/grant", response_model=ApprovalResponse)
async def grant_approval(
    request_id: str,
    body: GrantRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """审批通过。

    流程：
    1. 校验权限（群主 / 私信目标本人）
    2. ApprovalService.grant(executor=PermissionToolsMixin._execute_approved)
       - executor 重算 checksum，与 request 时的对比
       - 一致 → 执行 file_write/edit，返回 OK
       - 不一致 → 返回 Error（status 变 CONFLICT）
    3. 同步通知 Agent（IRQ granted/conflict）
    """
    req = ApprovalService.instance().get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    _user_can_decide(req, user_id, db)

    if req.status != ApprovalStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Approval already {req.status}",
        )
    if req.is_expired():
        raise HTTPException(status_code=410, detail="Approval request expired")

    decided_by = body.decided_by or f"user:{user_id}"

    # 调用 grant_and_execute —— 会触发 TOCTOU 检查 + 执行 + IRQ 通知
    try:
        updated = await grant_and_execute(request_id, decided_by)
    except KeyError:
        raise HTTPException(status_code=404, detail="Approval request not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    checksum_match = updated.status == ApprovalStatus.GRANTED
    if updated.status == ApprovalStatus.CONFLICT:
        # 409 表达文件冲突
        raise HTTPException(
            status_code=409,
            detail=f"文件已被修改（checksum 不一致）: {updated.error}",
        )
    return _req_to_response(updated, checksum_match=checksum_match)


@router.post("/{request_id}/deny", response_model=ApprovalResponse)
async def deny_approval(
    request_id: str,
    body: DenyRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """审批拒绝。

    流程：
    1. 校验权限
    2. ApprovalService.deny(reason=...)
    3. 同步通知 Agent（IRQ denied）
    """
    req = ApprovalService.instance().get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    _user_can_decide(req, user_id, db)

    if req.status != ApprovalStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Approval already {req.status}",
        )

    decided_by = body.decided_by or f"user:{user_id}"
    updated = await ApprovalService.instance().deny(
        request_id=request_id,
        decided_by=decided_by,
        reason=body.reason,
    )
    return _req_to_response(updated)


__all__ = ["router"]