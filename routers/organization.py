"""
Organization REST API — Phase 4

- POST   /api/organizations                  — 创建组织
- GET    /api/organizations                  — 列出当前用户的组织
- DELETE /api/organizations/{id}             — 软删除组织（owner only）
- PUT    /api/groups/{id}/bind-org           — 群绑定到组织
- PUT    /api/groups/{id}/unbind-org         — 群取消绑定

组织模型：organizations + Group.organization_id 外键。
绑定后，子群的 /mnt/group/{id}/.ref/_organization/ 显示该组织 .ref/。
"""

import logging
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db
from models.organization import Organization
from models.group import Group
from utils.auth import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Organizations"])


# ──────────── Schemas ────────────


class CreateOrganizationRequest(BaseModel):
    name: str
    description: Optional[str] = ""


class OrganizationResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = ""
    owner_user_id: int
    write_policy: str
    created_at: int

    class Config:
        from_attributes = True


class BindOrgRequest(BaseModel):
    organization_id: int


class BindOrgResponse(BaseModel):
    group_id: int
    organization_id: int


# ──────────── Helpers ────────────


def _format_org(o: Organization) -> OrganizationResponse:
    return OrganizationResponse(
        id=o.id,
        name=o.name,
        description=o.description or "",
        owner_user_id=o.owner_user_id,
        write_policy=o.write_policy or "owner_only",
        created_at=int(o.created_at.timestamp()) if o.created_at else 0,
    )


# ──────────── Org CRUD ────────────


@router.post("/api/organizations", response_model=OrganizationResponse)
async def create_organization(
    body: CreateOrganizationRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """创建组织（创建者自动成为 owner）。"""
    name = (body.name or "").strip()
    if not name or len(name) > 100:
        raise HTTPException(status_code=400, detail="Organization name must be 1-100 chars")
    org = Organization(
        name=name,
        description=(body.description or "").strip(),
        owner_user_id=user_id,
        write_policy="owner_only",
        created_at=datetime.utcnow(),
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    logger.info(f"[Organizations] created id={org.id} name={name} owner={user_id}")
    return _format_org(org)


@router.get("/api/organizations", response_model=List[OrganizationResponse])
async def list_organizations(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """列出当前用户创建的组织。"""
    rows = (
        db.query(Organization)
        .filter(
            Organization.owner_user_id == user_id,
            Organization.deleted_at.is_(None),
        )
        .order_by(Organization.created_at.desc())
        .all()
    )
    return [_format_org(o) for o in rows]


@router.delete("/api/organizations/{organization_id}")
async def delete_organization(
    organization_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """软删除组织（owner only）。关联群的 organization_id 自动解除。"""
    org = db.query(Organization).filter(
        Organization.id == organization_id,
        Organization.deleted_at.is_(None),
    ).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if org.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the owner can delete")

    # 解绑所有关联群
    bound_groups = (
        db.query(Group)
        .filter(Group.organization_id == organization_id, Group.deleted_at.is_(None))
        .all()
    )
    for g in bound_groups:
        g.organization_id = None
        g.updated_at = datetime.utcnow()

    org.deleted_at = datetime.utcnow()
    db.commit()
    logger.info(
        f"[Organizations] deleted id={organization_id} "
        f"unbound {len(bound_groups)} groups"
    )
    return {"status": "ok", "unbound_groups": len(bound_groups)}


# ──────────── Group ↔ Org ────────────


@router.put("/api/groups/{group_id}/bind-org", response_model=BindOrgResponse)
async def bind_group_to_org(
    group_id: int,
    body: BindOrgRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """把群绑定到组织（群主操作）。"""
    group = db.query(Group).filter(
        Group.id == group_id, Group.deleted_at.is_(None)
    ).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the group owner can bind")

    org = db.query(Organization).filter(
        Organization.id == body.organization_id,
        Organization.deleted_at.is_(None),
    ).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    if org.owner_user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot bind to an organization you don't own",
        )

    group.organization_id = org.id
    group.updated_at = datetime.utcnow()
    db.commit()
    logger.info(f"[Organizations] bound group={group_id} → org={org.id}")
    return BindOrgResponse(group_id=group.id, organization_id=org.id)


@router.put("/api/groups/{group_id}/unbind-org", response_model=BindOrgResponse)
async def unbind_group_from_org(
    group_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """群解除与组织的绑定（群主操作）。"""
    group = db.query(Group).filter(
        Group.id == group_id, Group.deleted_at.is_(None)
    ).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the group owner can unbind")
    if group.organization_id is None:
        raise HTTPException(status_code=400, detail="Group is not bound to any organization")

    old_org_id = group.organization_id
    group.organization_id = None
    group.updated_at = datetime.utcnow()
    db.commit()
    logger.info(f"[Organizations] unbound group={group_id} from org={old_org_id}")
    return BindOrgResponse(group_id=group.id, organization_id=0)


__all__ = ["router"]