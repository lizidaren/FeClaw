"""
FeHub REST API — Phase 6 Engine

Endpoints:
  GET    /api/fehub/apps              — list published apps for current user
  GET    /api/fehub/apps/{app_id}    — get a specific published app

  POST   /api/fehub/apps/{app_id}/data  — set app data key-value
  GET    /api/fehub/apps/{app_id}/data  — get app data (key= or prefix=)
  DELETE /api/fehub/apps/{app_id}/data  — delete app data (key= or prefix=)

Miniapp JS SDK compatible aliases (frontend uses these):
  POST   /apps/{agent_hash}/{app_id}/data
  GET    /apps/{agent_hash}/{app_id}/data
  DELETE /apps/{agent_hash}/{app_id}/data
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db, AgentProfile
from models.fehub import FePublish, AppData
from utils.auth import get_current_user_id
from services.fehub_service import FeHubService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["FeHub"])


# ── Request / Response Models ──────────────────────────────────


class AppDataSetRequest(BaseModel):
    key: str
    value: dict


class AppDataDeleteRequest(BaseModel):
    key: Optional[str] = None
    prefix: Optional[str] = None


class FePublishResponse(BaseModel):
    id: str
    app_name: str
    tag: str
    is_public: bool
    is_active: bool
    created_at: str

    class Config:
        from_attributes = True


class AppDataResponse(BaseModel):
    app_id: str
    user_id: int
    key: str
    value: dict


# ── Auth helper ─────────────────────────────────────────────────


def _get_user_agent(db: Session, user_id: int, agent_hash: str) -> AgentProfile:
    """Verify user owns this agent_hash."""
    agent = db.query(AgentProfile).filter(
        AgentProfile.hash == agent_hash,
        AgentProfile.user_id == user_id,
    ).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


# ── Published Apps ──────────────────────────────────────────────


@router.get("/api/fehub/apps")
async def list_fehub_apps(
    request: Request,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """List all published FeHub apps for the current user."""
    agents = db.query(AgentProfile).filter(
        AgentProfile.user_id == user_id,
    ).all()
    agent_hashes = [a.hash for a in agents]

    if not agent_hashes:
        return {"apps": []}

    publishes = db.query(FePublish).filter(
        FePublish.agent_hash.in_(agent_hashes),
        FePublish.is_active == True,
    ).order_by(FePublish.created_at.desc()).all()

    apps = []
    for p in publishes:
        app_id = f"{p.agent_hash}-{p.tag}"
        apps.append({
            "id": p.id,
            "app_id": app_id,
            "agent_hash": p.agent_hash,
            "app_name": p.app_name,
            "tag": p.tag,
            "is_public": p.is_public,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "url": f"https://{p.agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/",
        })

    return {"apps": apps}


@router.get("/api/fehub/apps/{app_id}")
async def get_fehub_app(
    app_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """Get a specific published app by app_id (agent_hash-tag format)."""
    parts = app_id.split("-", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Invalid app_id format")
    agent_hash, tag = parts

    _get_user_agent(db, user_id, agent_hash)

    publish = db.query(FePublish).filter(
        FePublish.agent_hash == agent_hash,
        FePublish.tag == tag,
    ).first()
    if not publish:
        raise HTTPException(status_code=404, detail="Published app not found")

    return {
        "id": publish.id,
        "app_id": app_id,
        "agent_hash": publish.agent_hash,
        "app_name": publish.app_name,
        "tag": publish.tag,
        "is_public": publish.is_public,
        "is_active": publish.is_active,
        "manifest": publish.manifest or {},
        "snapshot_path": publish.snapshot_path,
        "created_at": publish.created_at.isoformat() if publish.created_at else None,
        "url": f"https://{publish.agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/",
    }


# ── App Data (key-value store for miniapp frontend JS) ─────────


@router.post("/api/fehub/apps/{app_id}/data")
async def set_app_data(
    app_id: str,
    body: AppDataSetRequest,
    request: Request,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """Set a single key-value pair in app data store."""
    # app_id format: agent_hash-tag
    parts = app_id.rsplit("-", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Invalid app_id")
    agent_hash, tag = parts

    _get_user_agent(db, user_id, agent_hash)

    success = await FeHubService.set_app_data(
        app_id=app_id,
        user_id=user_id,
        key=body.key,
        value=body.value,
    )
    if success:
        return {"ok": True, "key": body.key}
    return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)


@router.get("/api/fehub/apps/{app_id}/data")
async def get_app_data(
    app_id: str,
    request: Request,
    key: Optional[str] = Query(None, description="Get specific key"),
    prefix: Optional[str] = Query(None, description="Get all keys with prefix"),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """Get app data. Use key= for single key, prefix= for all matching keys."""
    # app_id format: agent_hash-tag
    parts = app_id.rsplit("-", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Invalid app_id")
    agent_hash, tag = parts

    _get_user_agent(db, user_id, agent_hash)

    if not key and not prefix:
        raise HTTPException(status_code=400, detail="Provide key= or prefix= query param")

    result = await FeHubService.get_app_data(
        app_id=app_id,
        user_id=user_id,
        key=key,
        prefix=prefix,
    )
    return {"app_id": app_id, "data": result}


@router.delete("/api/fehub/apps/{app_id}/data")
async def delete_app_data(
    app_id: str,
    request: Request,
    key: Optional[str] = Query(None, description="Delete specific key"),
    prefix: Optional[str] = Query(None, description="Delete all keys with prefix"),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """Delete app data. Use key= for single key, prefix= for all matching keys."""
    parts = app_id.rsplit("-", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Invalid app_id")
    agent_hash, tag = parts

    _get_user_agent(db, user_id, agent_hash)

    if not key and not prefix:
        raise HTTPException(status_code=400, detail="Provide key= or prefix= query param")

    count = await FeHubService.delete_app_data(
        app_id=app_id,
        user_id=user_id,
        key=key,
        prefix=prefix,
    )
    return {"ok": True, "deleted": count}


# ── Miniapp JS SDK compatible aliases ───────────────────────────
# These are the endpoints the frontend miniapp JS calls directly.

@router.post("/apps/{agent_hash}/{app_id}/data")
async def miniapp_set_data(
    agent_hash: str,
    app_id: str,
    body: AppDataSetRequest,
    request: Request,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """Miniapp JS SDK: POST /apps/{agent_hash}/{app_id}/data"""
    full_app_id = f"{agent_hash}-{app_id}"
    _get_user_agent(db, user_id, agent_hash)

    success = await FeHubService.set_app_data(
        app_id=full_app_id,
        user_id=user_id,
        key=body.key,
        value=body.value,
    )
    return JSONResponse({"ok": success, "key": body.key}, status_code=200 if success else 500)


@router.get("/apps/{agent_hash}/{app_id}/data")
async def miniapp_get_data(
    agent_hash: str,
    app_id: str,
    request: Request,
    key: Optional[str] = Query(None),
    prefix: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """Miniapp JS SDK: GET /apps/{agent_hash}/{app_id}/data?key=... or ?prefix=..."""
    full_app_id = f"{agent_hash}-{app_id}"
    _get_user_agent(db, user_id, agent_hash)

    if not key and not prefix:
        raise HTTPException(status_code=400, detail="Provide key= or prefix= query param")

    result = await FeHubService.get_app_data(
        app_id=full_app_id,
        user_id=user_id,
        key=key,
        prefix=prefix,
    )
    return {"app_id": full_app_id, "data": result}


@router.delete("/apps/{agent_hash}/{app_id}/data")
async def miniapp_delete_data(
    agent_hash: str,
    app_id: str,
    request: Request,
    key: Optional[str] = Query(None),
    prefix: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """Miniapp JS SDK: DELETE /apps/{agent_hash}/{app_id}/data?key=..."""
    full_app_id = f"{agent_hash}-{app_id}"
    _get_user_agent(db, user_id, agent_hash)

    if not key and not prefix:
        raise HTTPException(status_code=400, detail="Provide key= or prefix= query param")

    count = await FeHubService.delete_app_data(
        app_id=full_app_id,
        user_id=user_id,
        key=key,
        prefix=prefix,
    )
    return {"ok": True, "deleted": count}
