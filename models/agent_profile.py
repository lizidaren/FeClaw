"""
AgentProfile 数据模型
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from models.database import Base


class AgentProfile(Base):
    """
    Agent 配置表

    每个 Agent 对应一个唯一的 4 位 hash
    """
    __tablename__ = "agent_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)  # 所属用户
    hash = Column(String(4), unique=True, nullable=False, index=True)  # 4 位十六进制
    totp_secret = Column(String(32), nullable=False)  # Base32 encoded secret
    name = Column(String(100), default="")  # Agent 名称
    description = Column(String(255), nullable=True)  # Agent 描述
    status = Column(String(20), default="pending")  # pending | initialized | suspended
    is_default = Column(Boolean, default=False)  # 是否为用户的默认 Agent
    permissions = Column(String(255), default="chat,upload,session")  # 权限列表（逗号分隔）
    agent_type = Column(String(20), default="classic")  # "classic" | "im"
    avatar_url = Column(String(512), nullable=True)  # Agent 头像 URL
    system_prompt = Column(Text, nullable=True)  # 自定义系统提示词模板
    parallel_sandbox = Column(Boolean, default=False)  # 是否允许多个并行 sandbox
    lock_behavior = Column(String(16), default="wait_3s")  # 文件锁行为: "eagain" | "wait_3s"
    sr_enabled = Column(Boolean, default=False)  # 是否启用 Smart Router
    is_pinned = Column(Boolean, default=False)  # Desktop 同步：是否置顶
    is_dnd = Column(Boolean, default=False)  # Desktop 同步：是否免打扰
    permission_mode = Column(String(32), nullable=True)  # Desktop 同步：权限模式
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True, onupdate=datetime.utcnow)
    initialized_at = Column(DateTime, nullable=True)
    configured_at = Column(DateTime, nullable=True)  # 配置页完成保存时间
    
    def __repr__(self):
        return f"<AgentProfile(hash={self.hash}, user_id={self.user_id}, status={self.status})>"
