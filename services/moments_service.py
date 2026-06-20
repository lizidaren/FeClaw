"""
GroupMoments Service - Phase 5 Engine
Handles GroupMoments CRUD and WebSocket push for moments events.
"""

import asyncio
import logging
import time
import uuid as _uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session

from models.database import SessionLocal
from models.group import GroupMoments, AgentProfile

logger = logging.getLogger(__name__)


class MomentsService:
    """Service for GroupMoments CRUD and WS push."""

    # ========== CRUD ==========

    def create_moment(
        self,
        db: Session,
        group_id: str,
        agent_hash: Optional[str],
        kind: str,
        title: Optional[str],
        content: Optional[str],
        attachments: Optional[List[Dict]] = None,
    ) -> GroupMoments:
        """Create a new group moment."""
        moment = GroupMoments(
            id=str(_uuid.uuid4()),
            group_id=group_id,
            agent_hash=agent_hash,
            kind=kind,
            title=title,
            content=content,
            attachments=attachments or [],
            created_at=datetime.utcnow(),
        )
        db.add(moment)
        db.commit()
        db.refresh(moment)
        logger.info(f"[Moments] Created {kind} moment id={moment.id} group={group_id} agent={agent_hash}")
        return moment

    def get_moments(
        self,
        db: Session,
        group_id: str,
        before: Optional[datetime] = None,
        limit: int = 50,
    ) -> List[GroupMoments]:
        """Get moments for a group, newest first."""
        query = db.query(GroupMoments).filter(GroupMoments.group_id == group_id)
        if before:
            query = query.filter(GroupMoments.created_at < before)
        return (
            query.order_by(GroupMoments.created_at.desc())
            .limit(limit)
            .all()
        )

    def get_user_moments(
        self,
        db: Session,
        user_id: int,
        group_id: Optional[str] = None,
        before: Optional[datetime] = None,
        limit: int = 50,
    ) -> List[GroupMoments]:
        """
        Get moments for all groups the user owns or is a member of.
        If group_id is provided, filter to that specific group.
        """
        from models.group import Group, GroupMember

        # Get group IDs the user has access to
        if group_id:
            group_ids = [group_id]
        else:
            owned = db.query(Group.id).filter(
                Group.owner_user_id == user_id,
                Group.deleted_at.is_(None)
            ).all()
            group_ids = [g.id for g in owned]

        if not group_ids:
            return []

        query = db.query(GroupMoments).filter(GroupMoments.group_id.in_(group_ids))
        if before:
            query = query.filter(GroupMoments.created_at < before)
        return (
            query.order_by(GroupMoments.created_at.desc())
            .limit(limit)
            .all()
        )

    def delete_moment(self, db: Session, moment_id: str, group_id: str, user_id: int) -> bool:
        """Delete a moment (owner or owning group's user only)."""
        from models.group import Group

        moment = db.query(GroupMoments).filter(
            GroupMoments.id == moment_id,
            GroupMoments.group_id == group_id,
        ).first()
        if not moment:
            return False

        # Check user owns the group
        group = db.query(Group).filter(Group.id == group_id).first()
        if not group or group.owner_user_id != user_id:
            return False

        db.delete(moment)
        db.commit()
        logger.info(f"[Moments] Deleted moment id={moment_id} group={group_id}")
        return True

    # ========== WS Push ==========

    async def push_moments_event(self, group_id: str, moment: GroupMoments):
        """Push a moments_event to connected Desktop WS clients."""
        try:
            from routers.desktop_ws import manager

            # Resolve agent_name from agent_hash
            agent_name = ""
            if moment.agent_hash:
                db = SessionLocal()
                try:
                    profile = db.query(AgentProfile).filter(
                        AgentProfile.hash == moment.agent_hash
                    ).first()
                    if profile:
                        agent_name = profile.name or profile.hash
                finally:
                    db.close()

            payload = {
                "type": "moments_event",
                "group_id": group_id,
                "moment": {
                    "id": moment.id,
                    "agent_hash": moment.agent_hash or "",
                    "agent_name": agent_name,
                    "kind": moment.kind,
                    "title": moment.title or "",
                    "content": moment.content or "",
                    "attachments": moment.attachments or [],
                    "created_at": int(moment.created_at.timestamp()),
                },
            }
            await manager.send(payload)
            logger.debug(f"[Moments] Pushed WS event for moment id={moment.id}")
        except Exception as e:
            logger.debug(f"[Moments] WS push skipped: {e}")

    # ========== Auto-publish helpers ==========

    def auto_publish(
        self,
        db: Session,
        group_id: str,
        agent_hash: Optional[str],
        kind: str,
        title: str,
        content: str,
        attachments: Optional[List[Dict]] = None,
    ):
        """
        Create and push a moment in one call (used by auto-publish hooks).
        This is synchronous; WS push is fire-and-forget.
        """
        moment = self.create_moment(
            db=db,
            group_id=group_id,
            agent_hash=agent_hash,
            kind=kind,
            title=title,
            content=content,
            attachments=attachments,
        )
        # Fire-and-forget WS push
        asyncio.create_task(self.push_moments_event(group_id, moment))
        return moment


# Global singleton
moments_service = MomentsService()
