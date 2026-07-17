"""
Agent ReplyBuffer 数据模型 - Agent V2

每个 Agent 拥有一个唯一的 ReplyBuffer 实例，用于暂存待发送的消息，
提供 TOCTOU 防护和多媒体附件一并发送的能力。
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, JSON
from sqlalchemy.sql import func

from models.database import Base


class AgentBuffer(Base):
    """
    Agent 回复缓冲区（每个 Agent 唯一一条记录）

    用于：
    - 暂存 Agent 待发送的消息（含附件）
    - Flush 前进行 TOCTOU 检查（防止发出"过时"消息）
    - Stash/Pop：临时切换话题时的暂存与恢复
    """
    __tablename__ = "agent_buffers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_hash = Column(String(8), unique=True, nullable=False, index=True)  # 唯一 buffer
    content = Column(Text, default="")
    attachments = Column(JSON, default=list)
    version = Column(Integer, default=0)  # 单调递增，每次 write 自增
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    # stash 支持：临时切换话题
    stash_content = Column(Text, nullable=True)
    stash_attachments = Column(JSON, nullable=True)
    stash_version = Column(Integer, nullable=True)
    stashed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<AgentBuffer(agent_hash={self.agent_hash}, version={self.version}, has_content={bool(self.content)})>"