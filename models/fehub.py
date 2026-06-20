"""
FeHub Models — VCS + Publish + AppData

FePublish: Published app records (snapshots stored in COS .fehub/releases/{tag}/)
AppData: Key-value runtime data for mini-apps (code vs data separation)
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON, UniqueConstraint, Index
from models.database import Base


class FePublish(Base):
    """Published app records — one per (agent_hash, tag)"""

    __tablename__ = "fe_publishes"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_hash = Column(String(4), nullable=False, index=True)
    app_name = Column(String(100), nullable=False)
    tag = Column(String(50), nullable=False)
    is_public = Column(Boolean, default=False)
    # snapshot stored in COS .fehub/releases/{tag}/
    snapshot_path = Column(Text, nullable=False)
    manifest = Column(JSON, default=dict)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("agent_hash", "tag", name="uq_fe_publish_agent_tag"),
        Index("idx_fe_publish_agent_hash", "agent_hash"),
    )


class AppData(Base):
    """Key-value runtime data for mini-apps — code vs data separation.

    Each app (app_id) can store per-user (user_id) key-value pairs.
    Used by miniapp frontend JS to persist state (settings, progress, etc.)
    """

    __tablename__ = "app_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    app_id = Column(String(36), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    key = Column(String(255), nullable=False)
    value = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("app_id", "user_id", "key", name="uq_app_data_app_user_key"),
        Index("idx_app_data_app_user", "app_id", "user_id"),
    )
