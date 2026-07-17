"""
Zentrim（格物所）数据模型

四层统一结构（每层 = { content, attachments[], metadata }），
存放在 ZentrimEntry.metadata_ 字段的 JSON 中：
- 原始层：用户原始产出
- 计算层：AI 处理结果
- 关联层：@引用关系
- 批注层：Agent/用户补充

ZentrimBlock 表存储条目的实际内容块（text/ink/audio/photo/image/file）。

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


# ────────────────────────────────────────────────────────────────────
# Block 类型白名单（迁移文档 §1.3）
# ────────────────────────────────────────────────────────────────────
BLOCK_TYPES = ("text", "ink", "audio", "photo", "image", "file")


class ZentrimEntry(Base):
    """Zentrim 条目表（精简版 — 实际内容存放在 ZentrimBlock 中）

    字段：
    - id / user_id / title / tags / status / metadata_ / 时间戳
    """
    __tablename__ = "zentrim_entries"
    __table_args__ = (
        Index("idx_zentrim_entries_user_created", "user_id", "created_at"),
        Index("idx_zentrim_entries_status", "user_id", "status"),
        CheckConstraint(
            "status IN ('active', 'archived', 'processing')",
            name="ck_zentrim_entries_status",
        ),
    )

    id = Column(String(26), primary_key=True)  # ULID
    user_id = Column(Integer, nullable=False, index=True)
    title = Column(Text, nullable=True)
    tags = Column(JSON, nullable=True)         # 字符串数组
    status = Column(String(16), default="active")  # active/archived/processing
    metadata_ = Column("metadata", JSON, nullable=True)  # 四层 + 所有扩展元数据
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    archived_at = Column(DateTime, nullable=True)


class ZentrimBlock(Base):
    """Zentrim 条目内容块（text/ink/audio/photo/image/file）

    设计要点：
    - 一个 ZentrimEntry 可包含 N 个 ZentrimBlock（按 sort_order 排序）
    - type 决定 data 字段的 schema（见迁移文档 §1.3）
    - text 字段为搜索用纯文本（来源视 type 而定）
    - vector_id 指向向量索引中的条目（未来 VLM/ASR 完成后写入）
    """
    __tablename__ = "zentrim_blocks"
    __table_args__ = (
        Index("idx_zentrim_blocks_entry", "entry_id"),
        Index("idx_zentrim_blocks_type", "type"),
        Index("idx_zentrim_blocks_entry_sort", "entry_id", "sort_order"),
        # MySQL FULLTEXT 索引（迁移文档 §1.2）：声明在 ORM 层时用 mysql_prefix="FULLTEXT"
        Index(
            "idx_zentrim_blocks_text_ft",
            "text",
            mysql_prefix="FULLTEXT",
        ),
    )

    id = Column(String(26), primary_key=True)              # ULID
    entry_id = Column(String(26), nullable=False, index=True)  # → ZentrimEntry.id
    sort_order = Column(Integer, nullable=False, default=0)
    type = Column(String(16), nullable=False)              # text/ink/audio/photo/image/file
    data = Column(JSON, nullable=True)                     # 类型专属数据
    text = Column(Text, nullable=True)                     # 搜索用文本
    model_name = Column(String(64), nullable=True)         # embedding 模型名
    vector_id = Column(String(64), nullable=True)          # 向量索引中的 ID
    created_at = Column(DateTime, default=datetime.utcnow)


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