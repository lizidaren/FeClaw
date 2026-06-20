"""
Group Dispatch Service - Phase 4 Engine
Group chat multi-agent dispatch and LLM reply orchestration
"""

import asyncio
import json
import logging
import time
import uuid as _uuid
from datetime import datetime
from typing import Optional, List, Dict, Any, Set

from sqlalchemy.orm import Session

from models.database import SessionLocal, AgentProfile
from models.group import Group, GroupMember, GroupMessage
from config import settings
from services.message_compactor import estimate_tokens

logger = logging.getLogger(__name__)

NO_REPLY_MAGIC = "NO_REPLY"


class GroupDispatchService:
    MAX_ROUNDS = 100
    CONTEXT_COMPACTION_THRESHOLD = 0.15  # keep 15% when compacting

    def __init__(self):
        self._running_tasks: Dict[str, asyncio.Task] = {}

    # ========== Entry Point ==========

    async def on_message(
        self,
        group_id: str,
        sender_type: str,
        sender_hash: str,
        content: str,
        mentions: Optional[List[str]] = None,
        attachments: Optional[List[Dict]] = None,
        message_type: str = "text",
    ) -> str:
        """
        Handle an incoming group message from any channel.

        Returns the message ID of the saved message.
        """
        # 1. Save GroupMessage to DB
        db = SessionLocal()
        try:
            msg_id = str(_uuid.uuid4())
            msg = GroupMessage(
                id=msg_id,
                group_id=group_id,
                sender_type=sender_type,
                sender_hash=sender_hash,
                content=content,
                message_type=message_type,
                attachments=attachments,
                mentions=mentions or [],
                round=0,
                created_at=datetime.utcnow(),
            )
            db.add(msg)
            db.commit()

            logger.info(
                f"[GroupDispatch] group={group_id} sender={sender_type}:{sender_hash} "
                f"content_len={len(content)}, mentions={mentions}"
            )

            # 2. Dispatch to members (fire-and-forget)
            asyncio.create_task(
                self.dispatch_to_members(group_id, round=0, exclude=sender_hash)
            )

            return msg_id
        finally:
            db.close()

    # ========== Dispatch ==========

    async def dispatch_to_members(
        self,
        group_id: str,
        round: int = 0,
        exclude: Optional[str] = None,
    ):
        """Dispatch agent replies to all members except sender."""
        # Guard: round limit
        if round >= self.MAX_ROUNDS:
            logger.info(f"[GroupDispatch] group={group_id} reached MAX_ROUNDS={self.MAX_ROUNDS}, stopping")
            return

        db = SessionLocal()
        try:
            members = (
                db.query(GroupMember)
                .filter(GroupMember.group_id == group_id)
                .all()
            )

            if not members:
                return

            for member in members:
                if member.agent_hash == exclude:
                    continue

                if self.should_wake(member, group_id, round):
                    task_key = f"{group_id}:{member.agent_hash}:{round}"
                    # Cancel any existing task for this slot
                    if task_key in self._running_tasks:
                        self._running_tasks[task_key].cancel()
                    self._running_tasks[task_key] = asyncio.create_task(
                        self.agent_reply(member.agent_hash, group_id, round)
                    )
        finally:
            db.close()

    def should_wake(self, member: GroupMember, group_id: str, round: int) -> bool:
        """
        Decide whether to wake an agent for this round.

        round==0       → always wake (fresh user message triggers all)
        member.is_silent → check mentions or message volume threshold
        otherwise       → wake
        """
        if round == 0:
            return True

        if member.is_silent:
            return False

        return True

    # ========== Agent Reply ==========

    async def agent_reply(self, agent_hash: str, group_id: str, round: int):
        """Generate and save an agent reply for one member."""
        task_key = f"{group_id}:{agent_hash}:{round}"
        try:
            logger.info(f"[GroupDispatch] agent_reply agent={agent_hash} group={group_id} round={round}")

            db = SessionLocal()
            try:
                # Build context
                context_messages, persona = self.build_context(agent_hash, group_id)

                if not context_messages:
                    logger.warning(f"[GroupDispatch] No context for agent={agent_hash}, skipping")
                    return

                # Build LLM prompt
                prompt = self._build_group_prompt(agent_hash, group_id, context_messages, persona)

                # Call LLM
                response = await self._call_llm(prompt, agent_hash)

                # Check NO_REPLY signal
                if response.strip() == NO_REPLY_MAGIC:
                    # Mark member as silent
                    member = (
                        db.query(GroupMember)
                        .filter(GroupMember.group_id == group_id, GroupMember.agent_hash == agent_hash)
                        .first()
                    )
                    if member:
                        member.is_silent = True
                        db.commit()
                    logger.info(f"[GroupDispatch] agent={agent_hash} returned NO_REPLY, marked silent")
                    return

                # Save reply as GroupMessage
                msg_id = str(_uuid.uuid4())
                reply_msg = GroupMessage(
                    id=msg_id,
                    group_id=group_id,
                    sender_type="agent",
                    sender_hash=agent_hash,
                    content=response,
                    message_type="text",
                    attachments=None,
                    mentions=[],
                    round=round,
                    created_at=datetime.utcnow(),
                )
                db.add(reply_msg)
                db.commit()

                logger.info(f"[GroupDispatch] agent={agent_hash} replied in group={group_id}, msg_id={msg_id}")

                # WS push to clients (fire-and-forget)
                asyncio.create_task(self._push_to_clients(group_id, msg_id, agent_hash, response))

                # Continue dispatch chain
                asyncio.create_task(
                    self.dispatch_to_members(group_id, round=round + 1, exclude=agent_hash)
                )
            finally:
                db.close()

        except asyncio.CancelledError:
            logger.info(f"[GroupDispatch] Task {task_key} cancelled")
            raise
        except Exception as e:
            logger.error(f"[GroupDispatch] agent_reply error: agent={agent_hash} group={group_id} {e}", exc_info=True)
        finally:
            self._running_tasks.pop(task_key, None)

    # ========== Context Building ==========

    def build_context(self, agent_hash: str, group_id: str) -> tuple:
        """
        Build chronological group history + agent personality for LLM prompt.

        Returns (messages_list, persona_str).
        """
        db = SessionLocal()
        try:
            # Load recent messages for this group (chronological)
            messages = (
                db.query(GroupMessage)
                .filter(GroupMessage.group_id == group_id)
                .order_by(GroupMessage.created_at.asc())
                .limit(200)
                .all()
            )

            if not messages:
                return [], ""

            # Build message list
            context_messages = []
            for msg in messages:
                role = "user" if msg.sender_type in ("user", "human") else "assistant"
                context_messages.append({
                    "role": role,
                    "content": msg.content or "",
                    "sender_hash": msg.sender_hash,
                })

            # Compact if > model window
            context_messages = self._compact_context(context_messages)

            # Load agent personality
            persona = ""
            try:
                from services.agent_init_service import agent_init_service
                persona = agent_init_service.load_agent_persona(agent_hash) or ""
            except Exception as e:
                logger.warning(f"[GroupDispatch] Failed to load persona for {agent_hash}: {e}")

            return context_messages, persona
        finally:
            db.close()

    def _compact_context(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Compact context to ~15% of recent messages when too large."""
        total_tokens = sum(estimate_tokens(m.get("content", "")) for m in messages)
        max_tokens = 110000 * self.CONTEXT_COMPACTION_THRESHOLD

        if total_tokens <= max_tokens:
            return messages

        # Keep last 15%
        keep_count = max(5, int(len(messages) * 0.15))
        compacted = messages[-keep_count:]
        logger.info(
            f"[GroupDispatch] Compacting context: {len(messages)} msgs → {len(compacted)} msgs"
        )
        return compacted

    def _build_group_prompt(
        self,
        agent_hash: str,
        group_id: str,
        context_messages: List[Dict[str, Any]],
        persona: str,
    ) -> str:
        """Build the system prompt for group chat."""
        import zoneinfo as _zi
        from datetime import datetime as _dt
        _tz = _zi.ZoneInfo("Asia/Shanghai")
        _now = _dt.now(_tz)

        lines = [
            f"【当前时间（BJT）】 {_now.year}.{_now.month}.{_now.day} {_now.hour:02d}:{_now.minute:02d}",
            "",
            "【群组聊天模式】",
            "你正在一个群组中与多个 AI Agent 和用户对话。",
            "请以你的角色身份，根据对话历史给出回复。",
            "如果某条消息明显是在单独对你说话，请重点回应。",
            "如果消息是群组闲聊，可以选择性回应或保持沉默。",
            "",
        ]

        if persona:
            lines.append("【你的人格设定】")
            lines.append(persona)
            lines.append("")

        lines.append("【群组消息历史】")
        for msg in context_messages:
            sender = msg.get("sender_hash", "?")
            role_label = "用户" if msg["role"] == "user" else "Agent"
            lines.append(f"[{role_label} {sender}]: {msg['content']}")

        lines.append("")
        lines.append("【回复规则】")
        lines.append("1. 如果你不需要回复（消息不针对你或无实质内容），请回复：NO_REPLY")
        lines.append("2. 否则，请以你的角色身份自然回复，不要声明你的思考过程。")

        return "\n".join(lines)

    # ========== LLM Call ==========

    async def _call_llm(self, prompt: str, agent_hash: str) -> str:
        """Call the LLM for a group chat response."""
        try:
            from services.llm_service import LLMService
            from services.model_registry import resolve as _resolve

            cfg = _resolve(settings.MAIN_TEXT_MODEL)
            llm = LLMService()

            full_response = ""
            async for chunk in llm.chat(
                messages=[{"role": "user", "content": prompt}],
                provider=cfg["provider"],
                model=settings.MAIN_TEXT_MODEL,
            ):
                full_response += chunk

            return full_response.strip()
        except Exception as e:
            logger.error(f"[GroupDispatch] LLM call failed for agent={agent_hash}: {e}", exc_info=True)
            return ""

    # ========== WS Push ==========

    async def _push_to_clients(
        self,
        group_id: str,
        msg_id: str,
        agent_hash: str,
        content: str,
    ):
        """Push a new group message to connected WS clients."""
        try:
            from routers.desktop_ws import manager
            payload = {
                "type": "group_message",
                "group_id": group_id,
                "msg_id": msg_id,
                "sender_type": "agent",
                "sender_hash": agent_hash,
                "content": content,
                "timestamp": int(time.time()),
            }
            await manager.send(payload)
        except Exception as e:
            logger.debug(f"[GroupDispatch] WS push skipped: {e}")

    # ========== CRUD Helpers ==========

    def create_group(
        self,
        db: Session,
        name: str,
        owner_user_id: int,
        member_hashes: Optional[List[str]] = None,
        settings: Optional[Dict] = None,
    ) -> Group:
        """Create a new group and add owner as first member."""
        group = Group(
            name=name,
            owner_user_id=owner_user_id,
            settings=settings or {},
            created_at=datetime.utcnow(),
        )
        db.add(group)
        db.flush()

        # Add owner as first member
        owner_member = GroupMember(
            group_id=group.id,
            agent_hash="",  # owner is user, not agent
            role="owner",
            is_silent=False,
            joined_at=datetime.utcnow(),
        )
        db.add(owner_member)

        # Add initial agent members
        if member_hashes:
            for h in member_hashes:
                db.add(GroupMember(
                    group_id=group.id,
                    agent_hash=h,
                    role="member",
                    is_silent=False,
                    joined_at=datetime.utcnow(),
                ))

        db.commit()
        db.refresh(group)
        logger.info(f"[GroupDispatch] Created group id={group.id} name={name} owner={owner_user_id}")
        return group

    def add_member(
        self,
        db: Session,
        group_id: str,
        agent_hash: str,
        role: str = "member",
    ) -> GroupMember:
        """Add an agent to a group."""
        existing = (
            db.query(GroupMember)
            .filter(GroupMember.group_id == group_id, GroupMember.agent_hash == agent_hash)
            .first()
        )
        if existing:
            return existing

        member = GroupMember(
            group_id=group_id,
            agent_hash=agent_hash,
            role=role,
            is_silent=False,
            joined_at=datetime.utcnow(),
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        logger.info(f"[GroupDispatch] Added member agent={agent_hash} to group={group_id}")
        return member

    def remove_member(self, db: Session, group_id: str, agent_hash: str) -> bool:
        """Remove an agent from a group."""
        member = (
            db.query(GroupMember)
            .filter(GroupMember.group_id == group_id, GroupMember.agent_hash == agent_hash)
            .first()
        )
        if not member:
            return False
        db.delete(member)
        db.commit()
        logger.info(f"[GroupDispatch] Removed member agent={agent_hash} from group={group_id}")
        return True

    def get_messages(
        self,
        db: Session,
        group_id: str,
        before: Optional[datetime] = None,
        limit: int = 50,
    ) -> List[GroupMessage]:
        """Get group messages, newest first."""
        query = db.query(GroupMessage).filter(GroupMessage.group_id == group_id)
        if before:
            query = query.filter(GroupMessage.created_at < before)
        return (
            query.order_by(GroupMessage.created_at.desc())
            .limit(limit)
            .all()
        )

    def get_group(self, db: Session, group_id: str) -> Optional[Group]:
        return db.query(Group).filter(Group.id == group_id).first()

    def get_member(
        self,
        db: Session,
        group_id: str,
        agent_hash: str,
    ) -> Optional[GroupMember]:
        return (
            db.query(GroupMember)
            .filter(GroupMember.group_id == group_id, GroupMember.agent_hash == agent_hash)
            .first()
        )

    def list_user_groups(self, db: Session, user_id: int) -> List[Group]:
        """List all groups owned by or joined by a user (via agents)."""
        # Groups where user is owner
        owned = db.query(Group).filter(Group.owner_user_id == user_id, Group.deleted_at.is_(None)).all()
        return owned


# Global singleton
group_dispatch_service = GroupDispatchService()