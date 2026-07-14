"""
Zentrim（格物所）数据模型

四层统一结构（每层 = { content, attachments[], metadata }）：
- 原始层：用户原始产出
- 计算层：AI 处理结果
- 关联层：@引用关系
- 批注层：Agent/用户补充

对应 PRD/TDD 参考 `docs/v1/02-zentrim.md`。
"""
import logging
from sqlalchemy import (
    CheckConstraint,
    Column,
    String,
    Text,
    Integer,
    DateTime,
    JSON,
    Index,
)
from datetime import datetime

from models.database import Base

logger = logging.getLogger(__name__)


class ZentrimEntry(Base):
    """Zentrim 条目表（note/photo/recording/link/canvas）"""
    __tablename__ = "zentrim_entries"
    __table_args__ = (
        Index("idx_zentrim_entries_user_created", "user_id", "created_at"),
        Index("idx_zentrim_entries_user_type", "user_id", "type"),
        Index("idx_zentrim_entries_status", "user_id", "status"),
        # fix(P2-2): DB 层 CHECK 约束（MySQL 兼容），防止脏数据写入
        CheckConstraint(
            "type IN ('note', 'photo', 'recording', 'link', 'canvas')",
            name="ck_zentrim_entries_type",
        ),
        CheckConstraint(
            "status IN ('active', 'archived', 'processing')",
            name="ck_zentrim_entries_status",
        ),
    )

    id = Column(String(26), primary_key=True)  # ULID
    user_id = Column(Integer, nullable=False, index=True)
    type = Column(String(16), nullable=False)  # note/photo/recording/link/canvas
    title = Column(Text, nullable=True)
    content = Column(Text, nullable=True)  # 计算层纯文本
    summary = Column(Text, nullable=True)  # VLM 摘要
    tags = Column(JSON, nullable=True)  # 字符串数组
    status = Column(String(16), default="active")  # active/archived/processing
    source = Column(String(32), nullable=True)  # manual/photo/recording/link/canvas
    source_url = Column(Text, nullable=True)
    attachment = Column(JSON, nullable=True)  # {bucket, key, mime, size}
    bbox = Column(JSON, nullable=True)  # {x_min,y_min,x_max,y_max}
    metadata_ = Column("metadata", JSON, nullable=True)  # 原始/计算/关联/批注四层
    vector_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    archived_at = Column(DateTime, nullable=True)


class ZentrimTimeline(Base):
    """Zentrim 子时间线表"""
    __tablename__ = "zentrim_timelines"
    __table_args__ = (
        Index("idx_zentrim_timelines_user", "user_id"),
    )

    id = Column(String(26), primary_key=True)  # ULID
    user_id = Column(Integer, nullable=False, index=True)
    name = Column(String(128), nullable=True)
    description = Column(Text, nullable=True)
    type = Column(String(16), default="custom")  # auto/custom
    created_at = Column(DateTime, default=datetime.utcnow)


class ZentrimTimelineEntry(Base):
    """Zentrim 时间线-条目多对多关联表"""
    __tablename__ = "zentrim_timeline_entries"
    __table_args__ = (
        Index("idx_zentrim_timeline_entries_timeline", "timeline_id"),
        Index("idx_zentrim_timeline_entries_entry", "entry_id"),
    )

    timeline_id = Column(String(26), primary_key=True)
    entry_id = Column(String(26), primary_key=True)
    sort_order = Column(Integer, default=0)
    added_at = Column(DateTime, default=datetime.utcnow)


class ZentrimReference(Base):
    """Zentrim @引用关系表（entry↔entry）"""
    __tablename__ = "zentrim_references"
    __table_args__ = (
        Index("idx_zentrim_references_source", "source_id"),
        Index("idx_zentrim_references_target", "target_id"),
    )

    id = Column(String(26), primary_key=True)  # ULID
    source_id = Column(String(26), nullable=False, index=True)
    target_id = Column(String(26), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
