"""
Group Chat Models - Phase 4 Engine
"""

from sqlalchemy import Column, String, Boolean, DateTime, JSON, Text, Integer, Index, ForeignKey
from models.database import Base
import uuid
from datetime import datetime


class Group(Base):
    __tablename__ = "groups"

    # P1.x: id 改为 Integer autoincrement（UUID 丑陋且 LLM 易幻觉）
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    announcement = Column(Text, default="")
    announcement_updated_at = Column(DateTime, nullable=True)
    owner_user_id = Column(Integer, nullable=False, index=True)
    settings = Column(JSON, default=dict)
    context_isolation = Column(Boolean, default=True)
    max_rounds = Column(Integer, default=100)
    # P4: 群可绑定一个组织。绑定后 .ref/_organization/ 显示该组织共享的 .ref/
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", use_alter=True, name="fk_groups_organization"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_groups_owner_user_id", "owner_user_id"),
        Index("idx_groups_organization_id", "organization_id"),
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # P1.x: group_id 从 String(36) 改为 Integer
    group_id = Column(Integer, nullable=False, index=True)
    agent_hash = Column(String(8), nullable=False)
    role = Column(String(16), default="member")
    is_silent = Column(Boolean, default=False)
    joined_at = Column(DateTime, default=datetime.utcnow)

    # P1.x: Agent 在群里的工作描述（拉 Agent 进群时由用户填写/选模板）
    job_description = Column(Text, nullable=True)
    # P1.x: Agent 当前状态 — dormant(休眠)/busy(工作中)
    status = Column(String(16), default="dormant")
    # P1.x: 是否允许群成员私信该 Agent（群主设置）
    allow_dm = Column(Boolean, default=True)

    __table_args__ = (
        Index("idx_group_members_group_id", "group_id"),
        Index("idx_group_members_agent_hash", "agent_hash"),
    )


class GroupMessage(Base):
    __tablename__ = "group_messages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # P1.x: group_id 从 String(36) 改为 Integer
    group_id = Column(Integer, nullable=False, index=True)
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
    # P1.x: group_id 从 String(36) 改为 Integer
    group_id = Column(Integer, nullable=False, index=True)
    agent_hash = Column(String(8), nullable=True)
    kind = Column(String(32), nullable=False)
    title = Column(String(200))
    content = Column(Text)
    attachments = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_group_moments_group_id", "group_id"),
        Index("idx_group_moments_created_at", "created_at"),
    )