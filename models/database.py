"""
FeClaw 数据库模型定义
使用 SQLAlchemy ORM，仅支持 MySQL
"""

import logging
from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean, DateTime, Date, ForeignKey, Index, JSON, Numeric, UniqueConstraint
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, Session
from datetime import datetime

logger = logging.getLogger(__name__)

from config import settings, DATABASE_URL

# 创建 MySQL 引擎
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,  # MySQL 连接池健康检查
    pool_recycle=3600,   # MySQL 连接回收时间
    echo=settings.DEBUG,
)

# 创建会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 创建基类
Base = declarative_base()


class User(Base):
    """用户表"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    email = Column(String(128), unique=True, index=True, nullable=True)  # 可选邮箱
    # DEPRECATED: 保留以向后兼容；新逻辑请用 UserLink 关联表（多 Provider 支持）。
    # 迁移脚本 scripts/migrate-user-links.py 会把此列数据复制到 user_links 表。
    platform_user_id = Column(String(64), nullable=True, unique=True)  # Platform OAuth 用户 ID（deprecated）
    password_hash = Column(String(128), nullable=False)
    salt = Column(String(64), nullable=True)  # 仅 SHA-256 legacy 用户需要；bcrypt 用户为 NULL
    password_version = Column(Integer, default=1)  # 1=SHA-256+salt, 2=bcrypt；详见 utils.auth
    is_admin = Column(Boolean, default=False)
    tier = Column(String(20), default="pro")  # "pro" | "enterprise"
    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    chat_histories = relationship("ChatHistory", back_populates="user")
    uploaded_files = relationship("UploadedFile", back_populates="user")
    user_links = relationship("UserLink", back_populates="user")


class UserLink(Base):
    """用户外部身份关联表（OAuth/OIDC 多 Provider 支持）

    取代单字段 User.platform_user_id：一个 FeClaw User 可绑定多个外部身份
    （platform / google / github / 本地登录 ...），每个 (provider, provider_user_id)
    全局唯一。
    """
    __tablename__ = "user_links"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String(32), nullable=False)  # 如 "platform", "google", "github"
    provider_user_id = Column(String(128), nullable=False)  # 外部 Provider 中的用户 ID
    provider_username = Column(String(128), nullable=True)  # 外部用户名缓存
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_provider_user"),
    )

    user = relationship("User", back_populates="user_links")


class UserWorkspace(Base):
    """用户工作空间表"""
    __tablename__ = "user_workspace"

    user_id = Column(String(32), primary_key=True, index=True)
    cos_bucket = Column(String(128), nullable=True)
    cos_prefix = Column(String(256), nullable=True)
    memory_sync_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class WeChatBinding(Base):
    """微信绑定表 - 存储 Agent 与微信的绑定关系（一个微信用户绑定一个 Agent）"""
    __tablename__ = "wechat_binding"
    __table_args__ = (
        Index("idx_wechat_binding_agent_hash", "agent_hash"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # 所属用户（从 AgentProfile 获取）
    agent_hash = Column(String(8), nullable=False, index=True)  # 绑定的 Agent hash
    wx_openid = Column(String(64), nullable=False)
    bot_token = Column(String(128), nullable=False)
    ilink_bot_id = Column(String(64), nullable=True)
    ilink_user_id = Column(String(64), nullable=True)
    context_token = Column(String(256), nullable=True)
    base_url = Column(String(256), nullable=True)
    status = Column(String(32), nullable=True)
    bound_at = Column(DateTime, nullable=True)
    last_msg_at = Column(DateTime, nullable=True)
    ilink_token = Column(Text, nullable=True)
    session_reset_at = Column(DateTime, nullable=True)


class WeChatMessage(Base):
    """微信消息表 - 存储微信消息记录（按 Agent 隔离）"""
    __tablename__ = "wechat_messages"
    __table_args__ = (
        Index("idx_wechat_messages_agent_hash", "agent_hash"),
    )

    id = Column(Integer, primary_key=True, index=True)
    binding_id = Column(Integer, ForeignKey("wechat_binding.id"), nullable=False)
    agent_hash = Column(String(8), nullable=False, index=True)  # Agent hash（便于查询）
    wx_openid = Column(String(64), nullable=False)
    direction = Column(String(16), nullable=False)  # "sent" 或 "received"
    content = Column(Text, nullable=False)
    message_type = Column(String(32), nullable=True)
    client_id = Column(Text, nullable=True)
    msg_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UploadedFile(Base):
    """已上传文件表（用于SHA1去重）"""
    __tablename__ = "uploaded_files"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    file_key = Column(String(256), unique=True, nullable=False)
    file_sha1 = Column(String(64), unique=True, nullable=False)
    cos_url = Column(String(512), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    user = relationship("User", back_populates="uploaded_files")


class FilePermission(Base):
    """文件权限表（按 Agent 隔离）"""
    __tablename__ = "file_permissions"
    __table_args__ = (
        Index("idx_file_permissions_agent_hash", "agent_hash"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(32), nullable=False, index=True)  # 所属用户（从 AgentProfile 获取）
    agent_hash = Column(String(8), nullable=False, index=True)  # Agent hash
    file_path = Column(String(512), nullable=False)
    permission = Column(String(16), nullable=False)  # "read", "write", "readwrite", "none"
    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class AgentConfig(Base):
    """Agent配置表"""
    __tablename__ = "agent_config"
    __table_args__ = (
        Index("idx_agent_config_hash", "agent_hash"),
        Index("idx_agent_config_hash_channel", "agent_hash", "channel"),
        UniqueConstraint("agent_hash", "key", name="uq_agent_config_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(128), nullable=False)  # 不再 unique，与 agent_hash 联合唯一
    value = Column(Text, nullable=True)
    agent_hash = Column(String(8), nullable=True)  # NULL = 全局配置
    channel = Column(String(32), nullable=True)
    permission = Column(String(16), default="readwrite")  # "none" | "read" | "readwrite"
    description = Column(String(255))
    updated_at = Column(DateTime, nullable=True)


class AgentUsageLog(Base):
    """Agent使用日志表（按 Agent 隔离）"""
    __tablename__ = "agent_usage_log"
    __table_args__ = (
        Index("idx_agent_usage_log_agent_hash", "agent_hash"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(32), nullable=False, index=True)  # 所属用户（从 AgentProfile 获取）
    agent_hash = Column(String(8), nullable=False, index=True)  # Agent hash
    provider = Column(String(32), nullable=True)
    model = Column(String(64), nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cached = Column(Boolean, nullable=True)
    cost_yuan = Column(Numeric(10, 4), nullable=True)
    created_at = Column(DateTime, nullable=True)


class ConversationSession(Base):
    """对话会话表（按 Agent 隔离）

    Gen 2 (IM Agent) 路由：
    - 同一 (agent_hash, channel) 组合在 IM Agent 下视为同一会话（无显式 session_id）
    - 唯一约束：(agent_hash, channel) — 同一 IM Agent 的同一渠道只对应一个会话
    - 索引：(agent_hash, channel) — 按渠道快速查找会话
    """
    __tablename__ = "conversation_sessions"
    __table_args__ = (
        Index("idx_conversation_sessions_agent_hash", "agent_hash"),
        Index("idx_conversation_sessions_agent_channel", "agent_hash", "channel"),
        UniqueConstraint("agent_hash", "channel", name="uq_session_agent_channel"),
    )

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(64), nullable=False, unique=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)  # 所属用户（从 AgentProfile 获取）
    agent_hash = Column(String(8), nullable=False, index=True)  # Agent hash
    channel = Column(String(32), nullable=True)  # Gen 2: 渠道名（web/mobile），classic Agent 保留 NULL
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    messages = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    topic = Column(String(64), nullable=True)
    importance = Column(Integer, default=3)
    message_count = Column(Integer, default=0)
    token_count = Column(Integer, default=0)
    is_archived = Column(Boolean, default=False)


class ScheduledTask(Base):
    """定时任务表（按 Agent 隔离）"""
    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        Index("idx_scheduled_tasks_agent_hash", "agent_hash"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(32), nullable=False, index=True)  # 所属用户（从 AgentProfile 获取）
    agent_hash = Column(String(8), default="")
    task_type = Column(String(16))  # "reminder" | "task"
    content = Column(Text)
    scheduled_at = Column(DateTime)
    status = Column(String(16), default="pending")  # pending | done | cancelled
    context_messages = Column(Text, nullable=True)  # JSON
    cron_expression = Column(String(64), nullable=True)
    pre_status = Column(String(16), default="pending")  # pending | pre_generated | failed
    pre_generated_content = Column(Text, nullable=True)
    pre_generate_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    # === 新增字段 ===
    output_mode = Column(String(16), default="session")   # "session" | "push" | "file"
    session_mode = Column(String(16), default="new")      # "current" | "new"
    pre_generate = Column(String(8), default="none")      # "none" | "1min" | "3min"
    source_session_id = Column(String(64), nullable=True)
    file_path = Column(String(512), nullable=True)
    channel = Column(String(32), nullable=True)           # 创建时自动记录


class UserPoints(Base):
    """用户积分表"""
    __tablename__ = "user_points"

    user_id = Column(String(32), primary_key=True, index=True)
    daily_points = Column(Integer, default=500)
    used_today = Column(Integer, default=0)
    last_reset = Column(Date, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class LLMStat(Base):
    """LLM调用统计表"""
    __tablename__ = "llm_stats"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String(32))
    model = Column(String(64))
    tokens_used = Column(Integer, default=0)
    request_type = Column(String(32))
    created_at = Column(DateTime, default=datetime.utcnow)


class ShareMapping(Base):
    """分享链接映射表（按 Agent 隔离）"""
    __tablename__ = "share_mappings"
    __table_args__ = (
        Index("idx_share_mappings_agent_hash", "agent_hash"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(32), nullable=False)  # 所属用户（从 AgentProfile 获取）
    agent_hash = Column(String(8), nullable=False, index=True)  # Agent hash
    vfs_path = Column(String(512), nullable=False)
    share_hash = Column(String(16), nullable=False, index=True)
    slug = Column(String(64), nullable=True, index=True)  # 友好短链（如"春风-明月-星辰"），代码层保证唯一
    mode = Column(String(16), nullable=False)
    password = Column(String(128), nullable=True)
    created_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)


def cleanup_expired_share_mappings(db: Session) -> int:
    """清理过期的 ShareMapping 记录"""
    from datetime import datetime
    now = datetime.utcnow()
    deleted = db.query(ShareMapping).filter(
        ShareMapping.expires_at.isnot(None),
        ShareMapping.expires_at < now
    ).delete()
    if deleted:
        db.commit()
    return deleted


class ShareReference(Base):
    """分享页引用令牌表 — 记录用户在分享页选中的文本片段"""
    __tablename__ = "share_references"
    __table_args__ = (
        Index("idx_share_refs_hash_created", "share_hash", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    ref_hash = Column(String(8), unique=True, index=True, nullable=False)
    share_hash = Column(String(16), nullable=False, index=True)
    agent_hash = Column(String(8), nullable=True)  # nullable 兼容老数据
    vfs_path = Column(String(512), nullable=False)
    selected_text = Column(Text, nullable=False)
    context_before = Column(Text, default="")
    context_after = Column(Text, default="")
    creator_ip = Column(String(45), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StaticSite(Base):
    """静态网站表"""
    __tablename__ = "static_sites"
    __table_args__ = (
        Index("idx_static_sites_user_id", "user_id"),
        Index("idx_static_sites_subdomain", "subdomain"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(32), nullable=False)
    subdomain = Column(String(63), nullable=False, unique=True)
    root_path = Column(String(256), nullable=False)
    status = Column(String(32), default="active")
    custom_cname = Column(String(256), nullable=True)  # 用户自定义域名
    cname_verified = Column(Boolean, default=False)  # CNAME 是否已验证
    cname_verified_at = Column(DateTime, nullable=True)  # 验证时间
    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class StaticSiteUsage(Base):
    """静态网站使用统计表（按天汇总）"""
    __tablename__ = "static_site_usage"
    __table_args__ = (
        Index("idx_static_site_usage_site_date", "site_id", "date", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    site_id = Column(Integer, ForeignKey("static_sites.id"), nullable=False)
    date = Column(Date, nullable=False)  # 统计日期
    visit_count = Column(Integer, default=0)  # 访问次数
    bandwidth_bytes = Column(Integer, default=0)  # 带宽使用（字节）
    unique_ips = Column(Integer, default=0)  # 独立 IP 数
    request_count = Column(Integer, default=0)  # 请求数（包括静态资源）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StaticSiteVisitLog(Base):
    """静态网站访问日志表（详细记录）"""
    __tablename__ = "static_site_visit_logs"
    __table_args__ = (
        Index("idx_static_site_visit_logs_site_created", "site_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    site_id = Column(Integer, ForeignKey("static_sites.id"), nullable=False)
    file_path = Column(String(512), nullable=False)  # 访问的文件路径
    client_ip = Column(String(64), nullable=True)  # 客户端 IP
    user_agent = Column(String(512), nullable=True)  # User-Agent
    referer = Column(String(512), nullable=True)  # 来源页面
    response_size = Column(Integer, default=0)  # 响应大小（字节）
    response_status = Column(Integer, default=200)  # HTTP 状态码
    created_at = Column(DateTime, default=datetime.utcnow)


class ChatHistory(Base):
    """AI对话记录表（按 Agent 隔离）"""
    __tablename__ = "chat_history"
    __table_args__ = (
        Index("idx_chat_history_agent_hash", "agent_hash"),
        # 复合索引：按 (agent_hash, channel, session_id, created_at) 加速历史加载
        Index("idx_chat_history_load", "agent_hash", "channel", "session_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)  # 所属用户（从 AgentProfile 获取）
    agent_hash = Column(String(8), nullable=False, index=True)  # Agent hash
    # role: "user" | "assistant" | "tool"
    # - "user": 用户消息
    # - "assistant": LLM 输出的纯文本回复；如有 tool_call，会在同一行的 content+tool_name+tool_args+tool_call_id 字段中体现
    # - "tool": 工具执行结果；通过 tool_call_id 关联回上一条 assistant 中的 tool_call
    role = Column(String(16), nullable=False)  # user/assistant/tool
    content = Column(Text, nullable=False)
    # === 工具调用相关字段（仅 role="assistant" 带 tool_call 或 role="tool" 时使用） ===
    tool_call_id = Column(String(64), nullable=True, index=True)  # 工具调用 ID（关联 tool_call ↔ tool_result）
    tool_name = Column(String(64), nullable=True)  # 工具名称（仅 assistant/tool 行有值）
    tool_args = Column(JSON, nullable=True)  # 工具参数 JSON 对象（仅 assistant 行有值）
    # === 其它字段 ===
    channel = Column(String(16), nullable=True, default=None)  # web/wechat/feishu 消息来源
    session_id = Column(String(32), nullable=True, index=True)  # wechat_main / web_sess_abc
    attachments = Column(JSON, nullable=True)  # [{type, url, mime_type, description}]
    meta = Column(JSON, nullable=True)  # {wechat_metadata: {msg_id, client_id}, ...}
    wechat_msg_id = Column(String(64), nullable=True, index=True)  # 从 meta JSON 派生的索引列，用于去重
    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    user = relationship("User", back_populates="chat_histories")


class VocabularyWord(Base):
    """高考英语词汇表"""
    __tablename__ = "vocabulary_words"

    id = Column(Integer, primary_key=True, autoincrement=True)
    word = Column(String(100), nullable=False, index=True)
    pronunciation = Column(String(100), default="")
    part_of_speech = Column(String(20), default="")
    meaning = Column(Text, default="")
    tags = Column(String(200), default="gaokao-3500")
    created_at = Column(DateTime, default=datetime.utcnow)


class SystemConfig(Base):
    """系统配置表（动态配置）"""
    __tablename__ = "system_config"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(128), unique=True, nullable=False)
    value = Column(Text)
    description = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Sandbox token table for cross-process authentication
class SandboxToken(Base):
    """Sandbox token shared across application instances."""
    __tablename__ = 'sandbox_tokens'

    token = Column(String(64), primary_key=True)
    agent_hash = Column(String(8), nullable=False, index=True)


class AgentTemplate(Base):
    """Agent 模板表"""
    __tablename__ = "agent_templates"

    id          = Column(String(32), primary_key=True)
    name        = Column(String(100), nullable=False)
    description = Column(String(500))

    definition  = Column(JSON, nullable=False)

    is_builtin  = Column(Boolean, default=False)
    sort_order  = Column(Integer, default=0)

    author_id   = Column(Integer, ForeignKey("users.id"), nullable=True)
    author_name = Column(String(100))

    version          = Column(String(20), default="1.0.0")
    feclaw_version   = Column(String(20))

    category    = Column(String(50))
    tags        = Column(JSON)
    language    = Column(String(10), default="zh-CN")
    icon        = Column(String(50))

    license     = Column(String(50), default="MIT")

    compliance_category  = Column(String(50), default="education")
    compliance_status    = Column(String(20), default="pending")
    compliance_reviewed_at = Column(DateTime, nullable=True)
    compliance_reason      = Column(String(500), nullable=True)

    created_at  = Column(DateTime)
    updated_at  = Column(DateTime)


# 创建所有表
def init_db() -> None:
    """初始化数据库"""
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created")


# 获取数据库会话
def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 导入 AgentProfile
from models.agent_profile import AgentProfile