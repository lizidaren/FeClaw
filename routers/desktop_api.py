"""Desktop 客户端专用 API 端点"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from models.database import get_db, AgentProfile
from utils.auth import get_current_user_id

router = APIRouter(tags=["Desktop API"])


@router.get("/api/desktop/agents")
async def list_agents(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """List all agents for the current user (Desktop client)."""
    agents = db.query(AgentProfile).filter(
        AgentProfile.user_id == user_id
    ).all()

    return [
        {
            "hash": a.hash,
            "name": a.name or a.hash,
            "description": a.description or "",
            "agent_type": a.agent_type or "classic",
            "avatar_url": a.avatar_url,
            "status": a.status or "pending",
            "is_default": a.is_default,
            "is_pinned": a.is_pinned or False,
            "is_dnd": a.is_dnd or False,
            "permission_mode": a.permission_mode,
        }
        for a in agents
    ]
