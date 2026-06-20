"""
Group Chat Models - Phase 4 Engine
"""

from sqlalchemy import Column, String, Boolean, DateTime, JSON, Text, Integer, Index
from models.database import Base
import uuid
from datetime import datetime


class Group(Base):
    __tablename__ = "groups"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    announcement = Column(Text, default="")
    announcement_updated_at = Column(DateTime, nullable=True)
    owner_user_id = Column(Integer, nullable=False, index=True)
    settings = Column(JSON, default=dict)
    context_isolation = Column(Boolean, default=True)
    max_rounds = Column(Integer, default=100)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_groups_owner_user_id", "owner_user_id"),
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(String(36), nullable=False, index=True)
    agent_hash = Column(String(4), nullable=False)
    role = Column(String(16), default="member")
    is_silent = Column(Boolean, default=False)
    joined_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_group_members_group_id", "group_id"),
        Index("idx_group_members_agent_hash", "agent_hash"),
    )


class GroupMessage(Base):
    __tablename__ = "group_messages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = Column(String(36), nullable=False, index=True)
    sender_type = Column(String(8), nullable=False)
    sender_hash = Column(String(4), nullable=True)
    content = Column(Text)
    message_type = Column(String(32), default="text")
    attachments = Column(JSON, nullable=True)
    mentions = Column(JSON, default=list)
    round = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_group_messages_group_id", "group_id"),
        Index("idx_group_messages_created_at", "created_at"),
    )


class GroupMoments(Base):
    __tablename__ = "group_moments"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = Column(String(36), nullable=False, index=True)
    agent_hash = Column(String(4), nullable=True)
    kind = Column(String(32), nullable=False)
    title = Column(String(200))
    content = Column(Text)
    attachments = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_group_moments_group_id", "group_id"),
        Index("idx_group_moments_created_at", "created_at"),
    )